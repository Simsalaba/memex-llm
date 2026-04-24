# memex-llm

Karpathy-style personal knowledge wiki. Distills your conversations into a persistent, cross-linked [Obsidian](https://obsidian.md/) vault — using local Ollama models, zero API cost.

```
ChatGPT export → triage (3B) → summarize (14B) → synthesize (14B) → Obsidian vault
                                    ↑                   ↑
                           resumable, parallel     community topic pages
                           enriched with entity graph
```

## Features

- **Fully local** — runs on Ollama, no cloud APIs, no data leaves your machine
- **Resumable** — checkpoint every conversation (and every community), kill and restart anytime
- **Three-pass architecture** — triage (3B) → summarize (14B) → synthesize (14B), each model stays hot throughout its pass
- **Community synthesis** — Graphify clusters conversations into topics; Pass 3 synthesizes one topic page per cluster
- **Parallel summarization** — distribute work across multiple machines over LAN (`--extra-url`)
- **Entity enrichment** — optional [Graphify](https://github.com/graphify) integration for wikilinks and cross-references
- **Map-reduce for large conversations and communities** — auto-detects content too long for a single LLM call

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com/) running locally
- GPU with enough VRAM for your chosen model (see [VRAM guide](#vram-guide))

## Installation

```bash
git clone https://github.com/Simsalaba/memex-llm
cd llm-wiki
pip install -e .
```

This installs the `wiki` CLI command.

## Configuration

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` — at minimum set your paths:

```yaml
paths:
  chatgpt_export: "~/chatgpt-export"   # folder with conversations-*.json
  vault: "~/llm-wiki-vault"            # where Obsidian pages are written
```

`config.yaml` is gitignored. Your personal paths and data never touch the repo.

### Environment variables

| Variable | Effect |
|----------|--------|
| `LLM_WIKI_CONFIG` | Path to a custom config file |
| `OLLAMA_BASE_URL` | Override `ollama.base_url` in config |

## Pull models

```bash
ollama pull qwen2.5:3b    # triage model (~2GB VRAM)
ollama pull qwen3:14b     # summary model (~9GB VRAM)
```

Any Ollama-compatible models work. See [model selection](#model-selection) for recommendations.

## Quick start

```bash
# Verify setup
wiki check

# Dry run — classify 50 conversations, no writes
wiki triage --limit 50

# Small batch — full pipeline on 20 conversations
wiki ingest chatgpt --limit 20

# Full ingest (resumable — Ctrl+C and restart anytime)
wiki ingest chatgpt
```

## All commands

```
wiki check                              verify Ollama + models (local + extra endpoints)
wiki status                             show checkpoint progress
wiki triage [DIR] [--limit N]           preview classifications, no writes

wiki ingest chatgpt [DIR]               full pipeline (two-pass, resumable)
  --triage-mode llm|programmatic|none
  --limit N                             process first N conversations only
  --extra-url URL                       add a remote Ollama endpoint (repeatable)
  --reindex-interval N                  regenerate index.md every N pages (default 100)

wiki synthesize                         pass 3 — synthesize community topic pages (vault/wiki/)
  --list                                list communities and their status
  --community SLUG                      synthesize one community only
  --reset SLUG|all                      re-queue for re-synthesis

wiki reindex                            regenerate index.md from existing vault (also writes _CLAUDE.md if missing)
wiki enrich --graph graph.json          re-enrich vault with a new Graphify graph (no LLM)
wiki reset-flagged                      re-queue too-short/empty conversations
wiki reset-trivial                      re-queue LLM-classified trivial conversations
wiki reset --all                        reset all to triage_done (re-summarize everything)
```

## Triage modes

| Mode | What happens | When to use |
|------|-------------|-------------|
| `llm` (default) | 3B model classifies each conversation | Normal ingest |
| `programmatic` | Rules only: flag if below min_messages/min_chars | No GPU for triage, or fast pre-filter |
| `none` | Skip triage, summarize everything | Small batches, testing, or resuming summarize pass only |

Flagged and trivial conversations are always recoverable via `wiki reset-flagged` / `wiki reset-trivial`.

## Multi-machine parallelization

Distribute the summarization pass across multiple machines on your LAN:

**On the remote machine** — expose Ollama over the network:
```bash
OLLAMA_HOST=0.0.0.0 ollama serve
ollama pull qwen3:14b
```

**Run with the extra endpoint:**
```bash
wiki ingest chatgpt --triage-mode none --extra-url http://192.168.1.x:11434
```

- The extra endpoint is probed at startup (5s timeout) — unreachable hosts are skipped automatically
- One worker thread per endpoint
- No data is stored on remote machines — prompts are sent over HTTP, only the summary text comes back
- Multiple `--extra-url` flags are supported

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Pass 1 — Triage (3B model, local only)                 │
│  Reads first 1500 chars of each conversation            │
│  → trivial (skip) / substantive / deep                  │
│  Checkpoint: marks each conv triage_done or trivial     │
└────────────────────┬────────────────────────────────────┘
                     │ non-trivial conversations
┌────────────────────▼────────────────────────────────────┐
│  Pass 2 — Summarize (14B model, parallel)               │
│  Short convs  → direct single LLM call                  │
│  Long convs   → map-reduce (chunk → extract → merge)    │
│  Extracts: summary, key_points, entities, tags, category│
│  Checkpoint: marks each conv written                    │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Wiki Writer                                            │
│  One Obsidian page per conversation (conversations/)    │
│  One aggregated page per entity (entities/)             │
│  Wikilinks enriched via Graphify graph (optional)       │
│  index.md regenerated every N pages                     │
└────────────────────┬────────────────────────────────────┘
                     │ run separately after ingest
┌────────────────────▼────────────────────────────────────┐
│  Pass 3 — Synthesize (14B model)           wiki ingest  │
│  Parses named communities from index.md                 │
│  Small communities → direct synthesis call              │
│  Large communities → map-reduce (batch → merge)         │
│  One evolving topic page per community (wiki/)          │
│  Separate checkpoint: data/synthesis_checkpoint.json    │
└─────────────────────────────────────────────────────────┘
```

### Checkpoint states

```
(new) → triage_done → written
      → trivial     (skipped, recoverable)
      → flagged     (too short, recoverable)
```

### Vault structure

```
vault/
├── _CLAUDE.md                  LLM navigation guide (auto-generated on first reindex)
├── index.md                    full catalog: conversations, wiki, notes
├── log.md                      append-only ingest log
├── conversations/
│   └── YYYY-MM/
│       └── conversation-title.md
├── entities/
│   └── entity-name.md
├── wiki/                       Pass 3 output — one synthesized topic page per community
│   └── community-slug.md
└── notes/                      manual session artifacts (decisions, discoveries, handoffs)
    └── YYYY-MM-DD-topic.md
```

## VRAM guide

Rule of thumb: weights + KV cache at your chosen `num_ctx`.

| Model | Weights | num_ctx=8192 | num_ctx=16384 | num_ctx=32768 |
|-------|---------|-------------|--------------|--------------|
| qwen2.5:3b | ~2GB | ~2.5GB | ~3GB | ~4GB |
| qwen3:14b / qwen2.5:14b | ~9GB | ~10.5GB | ~12GB | ~15GB |
| mistral-small3.1:24b | ~14GB | ~15.5GB | ~17GB | OOM on 16GB |

Set `num_ctx` in `config.yaml` — all other thresholds derive from it automatically.

Both models can run simultaneously if you have enough VRAM (e.g. 16GB handles 3b + 14b together). The two-pass design keeps each model loaded throughout its pass to avoid reload overhead.

## Model selection

Any model available in Ollama works. Suggested pairing:

| Use case | Triage | Summary |
|----------|--------|---------|
| 8GB VRAM | `qwen2.5:3b` | `qwen2.5:7b` |
| 12–16GB VRAM | `qwen2.5:3b` | `qwen3:14b` ← recommended |
| 24GB+ VRAM | `qwen2.5:3b` | `mistral-small3.1:24b` |
| CPU only | `qwen2.5:3b` | `qwen2.5:7b` (slow) |

## Adding a source parser

All parsers implement the same protocol — the rest of the pipeline is unchanged.

1. Create `pipeline/parsers/<source>.py`
2. Implement `parse(path) -> Iterator[Conversation]` (see `parsers/base.py` for the dataclass)
3. Add a CLI subcommand in `cli.py` under the `ingest` group (copy `ingest_chatgpt` as a template)

## Current limitations & roadmap

**Only ChatGPT exports are supported today.** The parser is pluggable — contributions welcome:

| Source | Status |
|--------|--------|
| ChatGPT export | ✅ Supported |
| Claude export | Planned |
| Slack export | Planned |
| Local markdown / notes | Planned |
| Browser history | Planned |
| GitHub issues / PRs | Planned |

The `Conversation` dataclass in `pipeline/parsers/base.py` is the only contract a new parser needs to satisfy.

### The `raw/` folder — next major milestone

The long-term goal is a `raw/` drop zone. Drop anything in it — a git repo, a PDF, a Slack export, a folder of markdown notes — and the pipeline figures out the rest.

```
raw/
├── my-project/        ← git repo
├── paper.pdf          ← research paper
├── slack-export/      ← Slack workspace dump
└── notes/             ← markdown files
        ↓
   type detector (deterministic — file signatures, directory structure)
        ↓
   source-specific extraction strategy
        ↓
   knowledge items → triage → summarize → Obsidian vault
```

Each source type needs its own extraction strategy — not just a parser, but an opinion on *what's worth extracting*. A git repo is the clearest example: Graphify can map its structure and relationships, but the vault needs to capture what modules *do*, why architectural decisions were made, and how the codebase evolved. That requires LLM extraction guided by repo-aware chunking (per-module, per-entry-point, git log narrative), not generic summarization.

The planned parser registry:

```python
PARSERS = {
    "chatgpt":  ChatGPTParser,       # ✅ done
    "git_repo": GitRepoParser,        # reads files + git log, LLM extracts per-module knowledge
    "pdf":      PDFParser,            # section-aware chunking
    "slack":    SlackParser,          # per-channel, per-thread
    "markdown": MarkdownParser,       # per-file or per-heading
    "unknown":  LLMFallbackParser,    # catch-all: LLM reads raw content, normalizes to KnowledgeItem
}
```

Type detection is deterministic. Parsing is deterministic. Only extraction uses an LLM — same models already running for triage and summarization.

## Inspiration & comparison

**[Andrej Karpathy](https://karpathy.ai/)** recently published his approach to a personal knowledge system built around MCP servers and AI agents. Seeing it was the push to finally build this. The goals are similar — a queryable, personal knowledge base grounded in your own data — but the approach is different:

| | Karpathy's approach | llm-wiki |
|--|---------------------|----------|
| **How it works** | MCP servers + agent instructions | Scripts + local Ollama |
| **Flexibility** | High — live, agent-driven | Lower — batch pipeline |
| **Cost** | Depends on model/API | Zero — fully local |
| **Best for** | Real-time, ongoing capture | Retroactive bulk ingestion of existing data |
| **Hardware** | Cloud or local | Local GPU recommended |

If you have years of ChatGPT history, Slack logs, or other accumulated data and want to process it all in one shot without ongoing API costs, this is the tool. If you want a live system that captures knowledge as you work, Karpathy's MCP approach is worth looking at.

The two approaches are complementary rather than competing — there is a plan to bridge them: use llm-wiki to retroactively ingest your historical data, then hand off to an MCP-based agent layer for ongoing live capture into the same vault. Watch this space.

**Graphify** — used to generate the entity relationship graph that powers wikilink enrichment and community detection. Run Graphify on your source data first to produce a `graph.json`, then point `paths.graphify_graph` at it. Without Graphify the pipeline still works — entity pages just won't have cross-references or community grouping.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[PolyForm Noncommercial 1.0](LICENSE) — free for personal, research, and educational use. Commercial use requires explicit permission from the author.
