# Roadmap

## Where we are

The pipeline has three passes:

1. **Triage** — 3B model classifies conversations (trivial / substantive / deep)
2. **Summarize** — 14B model extracts summary, key points, entities, tags per conversation
3. **Synthesize** — 14B model reads all conversation summaries per community cluster and writes one evolving topic page

The vault has four layers:

```
vault/
├── conversations/   archive — one page per conversation (lossless)
├── entities/        named entities extracted across all conversations
├── wiki/            synthesis — one topic page per community cluster
└── notes/           manual session artifacts via /vault-save
```

`_CLAUDE.md` at vault root gives an LLM the navigation map. `index.md` is the full catalog.

---

## Insight: why this architecture

Studied Karpathy's approach closely before settling on this. Key insight: the right model is **build-time synthesis + immutable archive**, not pure archive and not pure synthesis.

- Pure archive (conversations/ only): searchable but doesn't compound. You have 2000 pages about DevOps, not one page that knows everything you know about DevOps.
- Pure synthesis (rewrite pages on every ingest): lossy. Original context is gone once synthesized.
- This approach: keep the archive lossless, add a synthesis layer on top. Best of both.

Karpathy uses `raw/` (immutable) + `wiki/` (synthesized). We use `conversations/` + `wiki/` — same philosophy, different entry format (ChatGPT export vs manual ingest).

---

## What's next

### Immediate

**Run synthesis on all 72 communities**
```bash
wiki synthesize
```
Takes ~15-30 min on a 14B model. Resumable.

**Refresh `_CLAUDE.md` and index**
```bash
wiki reindex
```
First run after synthesis will generate `_CLAUDE.md` automatically.

---

### Near-term

**Handle Uncategorized (915 conversations)**

915 conversations (~43% of the dataset) have no Graphify community assignment because Graphify hit agent limits mid-run. Two strategies to unblock:

- **Wait for Graphify to finish** — once all chunks are processed, re-run `wiki enrich --graph graph.json` which will assign communities to remaining conversations, then re-synthesize.
- **LLM-assign** — for each uncategorized conversation, ask the 14B model "which of these 72 communities does this belong to?" and assign it. Then synthesize. This is a Pass 2.5.

Recommended: wait for Graphify first. LLM-assign as fallback if Graphify keeps hitting limits.

**Raw companion files**

During ingest, write a cleaned plaintext version of each conversation alongside the summary:
```
conversations/2024-03/
├── my-conversation.md        ← summary (existing)
└── my-conversation.raw.md   ← cleaned message text, stripped metadata
```

Pipeline already reads raw JSON — extracting clean text is near-zero cost. Gives LLMs a fallback when summary isn't enough, without storing noisy JSON in the vault.

Implementation: add `write_raw=True` option to `write_conversation_page()` in `wiki/writer.py`.

---

### Medium-term

**Claude Code session parser**

`wiki ingest chatgpt` handles ChatGPT exports. Claude Code sessions are not exported in the same format. Options:
- Use `CLAUDE.md` memory system (already in place via claude-mem) for cross-session continuity
- Add a Claude session export parser once Anthropic exposes an export format
- Alternatively: use `/vault-save` manually for high-value Claude sessions (current approach)

**Query layer**

When the vault grows large enough that `index.md` is too big to read in one LLM context, add semantic search:
- Embedding model: `ollama pull nomic-embed-text` (~274MB, free)
- Vector store: ChromaDB or Qdrant (local, no cloud)
- MCP server wrapping the vault for tool-use querying

At current scale (~2100 conversations, 72 wiki pages), `index.md` + direct file reads are sufficient — no embeddings needed. Revisit when wiki/ grows to 200+ pages or index.md exceeds 100k tokens.

**Re-synthesis on new ingests**

Currently `wiki synthesize` generates topic pages once. When new conversations are ingested, affected community pages go stale. Options:
- Reset and re-synthesize the affected communities after each ingest
- Incremental update: pass the existing wiki page + new conversation summaries to the LLM and ask it to update rather than rewrite

Incremental update is better (cheaper, preserves edits) but harder. Rewrite-on-change is simpler and correct. Implement rewrite first, optimize later.

**Additional source parsers**

The `Conversation` dataclass in `pipeline/parsers/base.py` is the only contract a new parser needs to satisfy.

| Source | Status | Notes |
|--------|--------|-------|
| ChatGPT export | ✅ Done | |
| Claude export | Planned | Needs export format from Anthropic |
| Slack export | Planned | Per-channel, per-thread |
| Local markdown / notes | Planned | Per-file or per-heading |
| PDF | Planned | Section-aware chunking |
| GitHub issues / PRs | Planned | |

---

### Long-term

**`raw/` drop zone**

Drop anything (git repo, PDF, Slack export, markdown folder) into `raw/` and the pipeline figures out the rest. Type detection is deterministic (file signatures, directory structure). Only extraction uses an LLM.

```
raw/
├── my-project/        ← git repo
├── paper.pdf
├── slack-export/
└── notes/
        ↓
   type detector
        ↓
   source-specific extractor
        ↓
   knowledge items → triage → summarize → synthesize → vault
```

**Vault as MCP server**

Expose the vault over MCP so any Claude Code session can query it:
- `search_vault(query)` → semantic search over wiki/ + entities/
- `get_topic(community)` → read a synthesis page
- `get_entity(name)` → read an entity page

This bridges the gap between historical batch knowledge (this pipeline) and real-time session context (Claude Code). The vault becomes a persistent memory layer accessible from any Claude session without re-ingesting anything.
