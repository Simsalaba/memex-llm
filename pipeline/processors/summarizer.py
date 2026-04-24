"""
Summarizer — deep summarization using the quality (14B) model.

Two strategies depending on conversation size:

1. Direct (≤ mapreduce_threshold chars):
   Single LLM call → structured JSON output.

2. Map-Reduce (> mapreduce_threshold chars):
   Pass 1 (map): split into overlapping chunks, extract key points from each.
   Pass 2 (reduce): merge all partial extractions → final structured JSON.
   Handles conversations up to any size (788k+ chars observed in dataset).

Output schema:
{
  "summary": "2-4 sentence distillation",
  "key_points": ["point 1", ...],
  "entities": [{"name": "...", "type": "tool|service|project|concept|person|org|hardware"}, ...],
  "tags": ["tag1", "tag2"],
  "category": "technical|creative|personal|research|planning|other"
}
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.ollama_client import OllamaClient, OllamaError
from pipeline.parsers.base import Conversation

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM = """\
You are a personal knowledge manager. Your job is to distill conversations into
structured wiki entries. Be concise and factual. Focus on lasting insights,
decisions made, and tools or concepts that matter to the user personally.
Always respond with valid JSON only — no markdown, no explanation outside the JSON."""

SUMMARIZE_PROMPT = """\
Summarize this conversation for a personal knowledge wiki.

Return ONLY valid JSON matching this exact schema:
{{
  "summary": "2-4 sentences capturing the core topic and outcome",
  "key_points": ["each point self-contained, max 10", "..."],
  "entities": [
    {{"name": "Entity Name", "type": "tool|service|project|concept|person|org|hardware"}}
  ],
  "tags": ["lowercase-hyphenated-tags", "max 8 tags"],
  "category": "technical|creative|personal|research|planning|other"
}}

Rules:
- summary: factual, no filler, written as if for future-you
- key_points: include decisions made, solutions found, or important facts learned
- entities: only named things (tools, people, projects) — not generic concepts
- tags: lowercase, hyphenated, broad topics
- category: pick the single best fit

Title: {title}
Date: {date}
Model: {model}
Triage: {triage}

--- Conversation ---
{text}
--- End ---

JSON:"""

# Map phase: extract key info from a single chunk (no JSON, faster)
CHUNK_EXTRACT_PROMPT = """\
You are reading part {chunk_num} of {total_chunks} of a conversation titled "{title}".
Extract the most important information from this segment only.

Return ONLY valid JSON:
{{
  "key_points": ["concise points from this segment, max 5"],
  "entities": [
    {{"name": "Entity Name", "type": "tool|service|project|concept|person|org|hardware"}}
  ],
  "topics": ["broad topic tags"]
}}

--- Segment {chunk_num}/{total_chunks} ---
{text}
--- End segment ---

JSON:"""

# Reduce phase: merge all partial extractions into final wiki summary
MERGE_PROMPT = """\
You are synthesizing extracted information from a long conversation titled "{title}" (date: {date}).
The conversation was split into {total_chunks} segments and key information was extracted from each.

Below are the extractions. Synthesize them into a final wiki entry.

Return ONLY valid JSON matching this exact schema:
{{
  "summary": "2-4 sentences capturing the overall topic and outcomes across the full conversation",
  "key_points": ["most important points across all segments, max 10, no duplicates"],
  "entities": [
    {{"name": "Entity Name", "type": "tool|service|project|concept|person|org|hardware"}}
  ],
  "tags": ["lowercase-hyphenated-tags", "max 8 tags"],
  "category": "technical|creative|personal|research|planning|other"
}}

--- Extracted segments ---
{extractions}
--- End ---

JSON:"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    summary: str
    key_points: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = "other"
    strategy: str = "direct"   # "direct" | "mapreduce"
    chunk_count: int = 1


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    Split text into overlapping chunks at message boundaries where possible.
    Falls back to hard char split if no boundary found.
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break

        # Try to split at a message boundary (double newline before "USER:" or "ASSISTANT:")
        boundary = end
        search_window = text[end - overlap : end]
        for marker in ["\n\nUSER:", "\n\nASSISTANT:", "\n\n"]:
            pos = search_window.rfind(marker)
            if pos != -1:
                boundary = (end - overlap) + pos
                break

        chunks.append(text[start:boundary])
        start = boundary - overlap  # overlap ensures continuity

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _direct_summarize(
    conv: Conversation,
    triage: str,
    client: OllamaClient,
    model: str,
    max_chars: int,
    num_ctx: int,
) -> Summary:
    """Single-call summarization for conversations that fit in context."""
    prompt = SUMMARIZE_PROMPT.format(
        title=conv.title,
        date=str(conv.date),
        model=conv.model or "unknown",
        triage=triage,
        text=conv.truncated_text(max_chars),
    )
    raw = client.generate_json(model, prompt, system=SUMMARIZE_SYSTEM, num_ctx=num_ctx)
    return _parse_summary(raw, strategy="direct")


def _mapreduce_summarize(
    conv: Conversation,
    triage: str,
    client: OllamaClient,
    model: str,
    chunk_size: int,
    overlap: int,
    num_ctx: int = 8192,   # chunks are small — use small ctx to save VRAM during map phase
) -> Summary:
    """
    Map-reduce summarization for large conversations.
    Pass 1 (map): extract key info from each chunk independently.
    Pass 2 (reduce): merge all extractions into final summary.
    """
    chunks = _chunk_text(conv.raw_text, chunk_size, overlap)
    total = len(chunks)

    # --- Map phase ---
    partial_results = []
    for i, chunk in enumerate(chunks, 1):
        prompt = CHUNK_EXTRACT_PROMPT.format(
            chunk_num=i,
            total_chunks=total,
            title=conv.title,
            text=chunk,
        )
        try:
            # Chunks are small — use small ctx (saves VRAM vs loading full window per chunk)
            chunk_ctx = min(num_ctx, 8192)
            result = client.generate_json(model, prompt, num_ctx=chunk_ctx)
            partial_results.append(result)
        except OllamaError:
            # If a chunk fails, skip it (better than aborting the whole conversation)
            partial_results.append({"key_points": [], "entities": [], "topics": []})

    # --- Reduce phase ---
    # Format extractions as readable text for the merge prompt
    extraction_lines = []
    for i, part in enumerate(partial_results, 1):
        pts = "\n".join(f"  - {p}" for p in part.get("key_points", []))
        ents = ", ".join(e.get("name", "") for e in part.get("entities", []) if isinstance(e, dict))
        extraction_lines.append(
            f"Segment {i}/{total}:\n"
            f"  Key points:\n{pts or '  (none)'}\n"
            f"  Entities: {ents or '(none)'}"
        )

    merge_prompt = MERGE_PROMPT.format(
        title=conv.title,
        date=str(conv.date),
        total_chunks=total,
        extractions="\n\n".join(extraction_lines),
    )

    # Merge prompt is small (just the extracted points) — 8192 ctx is plenty
    raw = client.generate_json(model, merge_prompt, system=SUMMARIZE_SYSTEM, num_ctx=8192)
    summary = _parse_summary(raw, strategy="mapreduce")
    summary.chunk_count = total
    return summary


def _parse_summary(raw: dict, strategy: str) -> Summary:
    return Summary(
        summary=str(raw.get("summary", "")).strip(),
        key_points=[str(p) for p in raw.get("key_points", []) if p],
        entities=[
            {"name": str(e.get("name", "")), "type": str(e.get("type", "concept"))}
            for e in raw.get("entities", [])
            if isinstance(e, dict) and e.get("name")
        ],
        tags=[str(t).lower().replace(" ", "-") for t in raw.get("tags", []) if t],
        category=str(raw.get("category", "other")),
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_conversation(
    conv: Conversation,
    triage: str,
    client: OllamaClient,
    model: str,
    max_chars: int = 48000,
    mapreduce_threshold: int = 48000,
    chunk_size: int = 20000,
    chunk_overlap: int = 500,
    num_ctx: int = 16384,
) -> Summary:
    """
    Summarize a conversation. Automatically selects strategy based on size:
    - direct: conversation fits in context window (≤ mapreduce_threshold chars)
    - mapreduce: conversation is too large, chunk and merge

    num_ctx controls VRAM usage. Defaults match config.yaml num_ctx: 16384.
    mapreduce_threshold should always be ≤ num_ctx * 3.5 * 0.85 to ensure the
    content actually fits in the context window.

    Returns a Summary dataclass with a 'strategy' field indicating which was used.
    """
    if conv.total_chars <= mapreduce_threshold:
        return _direct_summarize(conv, triage, client, model, max_chars, num_ctx=num_ctx)
    else:
        return _mapreduce_summarize(conv, triage, client, model, chunk_size, chunk_overlap, num_ctx=num_ctx)
