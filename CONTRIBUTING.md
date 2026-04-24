# Contributing

## Setup

```bash
git clone https://github.com/Simsalaba/memex-llm
cd llm-wiki
pip install -e .
cp config.example.yaml config.yaml
# edit config.yaml with your paths
```

## Project structure

```
pipeline/
├── cli.py              CLI commands (entry point)
├── config.py           Config loader
├── checkpoint.py       Resumable state tracking
├── ollama_client.py    Ollama HTTP client
├── graphify_bridge.py  Entity normalization + graph integration
├── parsers/            One file per data source
├── processors/         triage.py, summarizer.py
└── wiki/               writer.py, index.py, log.py
```

## Adding a parser (most common contribution)

1. Create `pipeline/parsers/<source>.py`
2. Implement `parse(path: str) -> Iterator[Conversation]`
   - `Conversation` is defined in `pipeline/parsers/base.py`
   - The only required fields are `id`, `title`, `date`, `source`, and `messages`
3. Add a subcommand under the `ingest` group in `cli.py` — copy `ingest_chatgpt` as a template
4. The triage, summarizer, writer, and checkpoint all work unchanged

## The `raw/` folder (next major feature — help wanted)

The goal is a drop zone where any data source is auto-detected and routed to the right extraction strategy. The architecture:

```
raw/ → TypeDetector → SourceStrategy → KnowledgeItems → existing pipeline
```

**TypeDetector** — deterministic, no LLM. Identifies source type from file signatures and directory structure:
- `.git/` present → `git_repo`
- `conversations-*.json` → `chatgpt`
- `channels.json` + message dirs → `slack`
- `.pdf` extension → `pdf`
- `.md` files → `markdown`
- anything else → `unknown` (LLM fallback)

**SourceStrategy** — knows *what to extract* from a given type, not just how to parse it. This is the interesting part. A git repo strategy, for example, would:
- Chunk by module/package, not by token count
- Run an LLM pass per module to extract "what this does and why"
- Summarize the git log as a narrative of how the project evolved
- Use Graphify's structural graph to enrich cross-references between modules

A PDF strategy would chunk by section headings. A Slack strategy would chunk by channel and thread. The existing summarizer handles the LLM step — the strategy's job is producing well-formed chunks with the right context.

**Contributing a strategy:**
1. Add a type signature to `pipeline/parsers/detector.py` (to be created)
2. Create `pipeline/strategies/<type>.py` implementing `extract(path) -> Iterator[KnowledgeItem]`
3. `KnowledgeItem` will be an extension of `Conversation` that also carries `source_type`, `chunk_strategy`, and optional structured metadata

The LLM fallback strategy (`unknown`) reads raw content and asks the model to normalize it — lower fidelity but handles anything.

## Key design rules

- **Resumable**: every operation must be checkpoint-safe. If a conversation is already `written` in the checkpoint, skip it.
- **No hardcoded paths**: all paths come from `config.yaml`. Use `cfg.get()["paths"]["..."]`.
- **Model-agnostic**: the pipeline uses `ollama_client.py` — any Ollama model works.
- **Fail gracefully**: errors on individual conversations are caught and logged; the run continues.

## Code style

- Python 3.12+, type hints on all public functions
- `rich` for all terminal output — no bare `print()`
- Config is loaded via `pipeline.config.get()` — never read `config.yaml` directly

## Running a test ingest

```bash
wiki check
wiki ingest chatgpt --limit 5 --triage-mode none
wiki status
```
