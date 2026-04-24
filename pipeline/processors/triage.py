"""
Triage processor — fast classification using the small (3B) model.

Two-stage:
  1. Programmatic pre-filter (no LLM): skip trivially short/empty conversations
  2. LLM classification: trivial | substantive | deep

Returns "trivial", "substantive", or "deep".
"""
from __future__ import annotations

from pipeline.ollama_client import OllamaClient
from pipeline.parsers.base import Conversation

TRIAGE_SYSTEM = """\
You are a knowledge curator. Classify conversations by their lasting value.
Respond with EXACTLY one word: trivial, substantive, or deep. No explanation."""

TRIAGE_PROMPT = """\
Classify this conversation by lasting knowledge value.

trivial = throwaway (tests, simple math, greetings, very short with no reusable insight, one-liner Q&A)
substantive = useful discussion worth recording — has some insight, decision, or learning
deep = significant discussion — multi-topic, complex reasoning, important decisions, or major learning

Title: {title}
Messages: {message_count}
Model: {model}

--- Conversation start ---
{head_text}
--- End ---

Classification (one word):"""

VALID_CLASSES = {"trivial", "substantive", "deep"}


def triage_programmatic(conv: Conversation, min_messages: int, min_chars: int) -> str | None:
    """
    Fast pre-filter before calling the LLM.
    Returns "flagged" if clearly too short/empty, None if LLM should decide.
    """
    if conv.message_count < min_messages:
        return "flagged"
    if conv.total_chars < min_chars:
        return "flagged"
    return None


def triage_programmatic_reason(conv: Conversation, min_messages: int, min_chars: int) -> str:
    """Return human-readable reason for programmatic flag."""
    if conv.message_count < min_messages:
        return f"only {conv.message_count} message(s) (min={min_messages})"
    if conv.total_chars < min_chars:
        return f"only {conv.total_chars} chars (min={min_chars})"
    return ""


def triage_conversation(
    conv: Conversation,
    client: OllamaClient,
    model: str,
    head_chars: int = 1500,
    min_messages: int = 2,
    min_chars: int = 100,
) -> str:
    """
    Classify a single conversation. Returns "trivial", "substantive", or "deep".
    """
    # Fast path: skip LLM if clearly trivial
    pre = triage_programmatic(conv, min_messages, min_chars)
    if pre is not None:
        return pre

    prompt = TRIAGE_PROMPT.format(
        title=conv.title,
        message_count=conv.message_count,
        model=conv.model or "unknown",
        head_text=conv.head_text(head_chars),
    )

    raw = client.generate(model, prompt, system=TRIAGE_SYSTEM, temperature=0.0)

    # Extract first word, lowercase
    word = raw.strip().split()[0].lower().rstrip(".,!?")
    if word not in VALID_CLASSES:
        # Default to substantive if model returns something unexpected
        return "substantive"

    return word
