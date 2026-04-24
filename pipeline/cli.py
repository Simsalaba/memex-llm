"""
LLM Wiki CLI

Usage:
  wiki ingest chatgpt <export_dir>      # full pipeline on ChatGPT export
  wiki triage chatgpt <export_dir>      # triage-only pass (preview before full run)
  wiki status                           # show checkpoint progress
  wiki reindex                          # regenerate index.md from vault
  wiki enrich --graph graph.json        # re-enrich vault with a new Graphify graph
  wiki check                            # verify Ollama is running + models available
"""
from __future__ import annotations

import queue as _queue
import signal as _signal
import sys
import threading as _threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as _futures_wait
from pathlib import Path

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from pipeline import config as cfg
from pipeline.checkpoint import Checkpoint
from pipeline.graphify_bridge import load_graph
from pipeline.ollama_client import OllamaClient
from pipeline.parsers.chatgpt import parse_export
from pipeline.processors.summarizer import summarize_conversation
from pipeline.processors.triage import (
    triage_conversation,
    triage_programmatic,
    triage_programmatic_reason,
)
from pipeline.processors.synthesizer import load_community_summaries, parse_communities, synthesize_community
from pipeline.wiki.enricher import enrich_vault
from pipeline.wiki.index import regenerate_index
from pipeline.wiki.log import append_log
from pipeline.wiki.synthesis_writer import SynthesisCheckpoint, write_synthesis_page
from pipeline.wiki.writer import update_entity_page, write_conversation_page

console = Console()


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """LLM Wiki — Karpathy-style knowledge base powered by Ollama."""


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--extra-url", "extra_urls_cli", multiple=True,
              help="Extra Ollama endpoint(s) to check. Overrides config extra_ollama_urls.")
def check(extra_urls_cli: tuple) -> None:
    """Verify Ollama is running and required models are available."""
    c = cfg.get()
    triage_model = c["ollama"]["triage_model"]
    summary_model = c["ollama"]["summary_model"]
    extra_urls = list(extra_urls_cli) if extra_urls_cli else c["ollama"].get("extra_ollama_urls", [])
    all_urls = [c["ollama"]["base_url"]] + extra_urls

    # ── Endpoint connectivity ────────────────────────────────────────────────
    ep_table = Table(title="Endpoints", show_header=True)
    ep_table.add_column("URL")
    ep_table.add_column("Status")
    ep_table.add_column("Summary model")

    any_primary_down = False
    for i, url in enumerate(all_urls):
        cl = OllamaClient(url, timeout=5.0)
        label = "primary" if i == 0 else "extra"
        if cl.is_available():
            try:
                models = cl.list_models()  # also uses 5s timeout
                has_model = any(summary_model in m for m in models)
                model_status = "[green]pulled" if has_model else f"[red]MISSING — ollama pull {summary_model}"
            except Exception:
                model_status = "[yellow]could not list models"
            ep_table.add_row(f"{url} ({label})", "[green]online", model_status)
        else:
            ep_table.add_row(f"{url} ({label})", "[red]unreachable", "[dim]—")
            if i == 0:
                any_primary_down = True

    console.print(ep_table)

    if any_primary_down:
        console.print("[red]Primary Ollama unreachable. Start with: ollama serve")
        sys.exit(1)

    # ── Local models ─────────────────────────────────────────────────────────
    primary = OllamaClient(c["ollama"]["base_url"])
    models = primary.list_models()

    model_table = Table(title="Local Models", show_header=True)
    model_table.add_column("Role")
    model_table.add_column("Model")
    model_table.add_column("Status")

    t_status = "[green]OK" if any(triage_model in m for m in models) else "[red]MISSING — run: ollama pull " + triage_model
    s_status = "[green]OK" if any(summary_model in m for m in models) else "[red]MISSING — run: ollama pull " + summary_model

    model_table.add_row("Triage", triage_model, t_status)
    model_table.add_row("Summarize", summary_model, s_status)
    console.print(model_table)

    if "[red]" in t_status or "[red]" in s_status:
        console.print("\n[yellow]Pull missing models before running ingest.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status() -> None:
    """Show ingest checkpoint progress."""
    c = cfg.get()
    cp = Checkpoint(Path(c["paths"]["checkpoint"]))
    stats = cp.stats()
    total = cp.total()

    if total == 0:
        console.print("[yellow]No checkpoint found. Run 'wiki ingest chatgpt <path>' to start.")
        return

    table = Table(title=f"Checkpoint ({total} conversations tracked)")
    table.add_column("Status")
    table.add_column("Count")
    table.add_column("Note")
    for status_key, note in [
        ("written", "done"),
        ("triage_done", "awaiting summarize"),
        ("trivial", "skipped by LLM — recoverable"),
        ("flagged", "skipped by rules — recoverable"),
        ("unknown", ""),
    ]:
        count = stats.get(status_key, 0)
        if count or status_key in ("written", "triage_done", "trivial", "flagged"):
            table.add_row(status_key, str(count), note)
    console.print(table)

    written = stats.get("written", 0)
    if total > 0:
        console.print(f"Progress: {written}/{total} ({100*written//total}% complete)")


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------

@cli.command()
def reindex() -> None:
    """Regenerate index.md from existing vault pages."""
    c = cfg.get()
    vault = Path(c["paths"]["vault"])
    console.print("Regenerating index.md...")
    regenerate_index(vault)
    console.print(f"[green]Done — index.md updated at {vault}/index.md")


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--graph", "graph_path", required=True,
              help="Path to a graphify graph.json to enrich the vault with.")
def enrich(graph_path: str) -> None:
    """
    Re-enrich the vault with a new Graphify graph.

    Run this after completing an ingest and generating a fresh graph from the vault output.
    Merges fragmented entity pages, updates wikilinks, and fixes community labels —
    no LLM calls, pure graph + file operations.
    """
    c = cfg.get()
    vault = Path(c["paths"]["vault"])

    if not vault.exists():
        console.print(f"[red]Vault not found at {vault}")
        sys.exit(1)

    graph_file = Path(graph_path).expanduser()
    if not graph_file.exists():
        console.print(f"[red]Graph file not found: {graph_path}")
        sys.exit(1)

    console.print(f"Loading graph from {graph_file}...")
    graph = load_graph(graph_file)
    console.print(f"[dim]{len(graph.nodes)} nodes, {len(graph.communities)} communities")

    conv_pages = list((vault / "conversations").rglob("*.md")) if (vault / "conversations").exists() else []
    console.print(f"\nEnriching {len(conv_pages)} conversation pages + entity pages...")

    done = 0

    def _progress(n: int, total: int) -> None:
        nonlocal done
        done = n
        progress.update(task, completed=n, total=total)

    with _make_progress() as progress:
        task = progress.add_task("Enriching...", total=len(conv_pages) or 1)
        stats = enrich_vault(vault, graph, progress_callback=_progress)

    table = Table(title="Enrich Complete")
    table.add_column("Stat")
    table.add_column("Value")
    table.add_row("Entity pages before", str(stats.entity_pages_before))
    table.add_row("Entity pages after",  str(stats.entity_pages_after))
    table.add_row("Entity pages merged", str(stats.entity_pages_merged))
    table.add_row("Conversation pages updated", str(stats.conversation_pages_updated))
    table.add_row("Conversation pages unchanged", str(stats.conversation_pages_skipped))
    console.print(table)

    console.print("\nRegenerating index.md...")
    regenerate_index(vault)
    console.print("[green]Done.")


# ---------------------------------------------------------------------------
# reset commands
# ---------------------------------------------------------------------------

@cli.command("reset-flagged")
def reset_flagged() -> None:
    """
    Re-queue programmatically flagged conversations for processing.

    Flagged = too short / too few messages (caught by rules, no LLM used).
    After reset, the next ingest run will re-triage them.
    """
    c = cfg.get()
    cp = Checkpoint(Path(c["paths"]["checkpoint"]))
    n = cp.reset_flagged()
    if n:
        console.print(f"[green]Reset {n} flagged conversation(s) — they will be re-triaged on next ingest.")
    else:
        console.print("[yellow]No flagged conversations in checkpoint.")


@cli.command("reset-trivial")
def reset_trivial() -> None:
    """
    Re-queue trivial + flagged conversations for processing.

    Trivial = LLM classified as not worth keeping.
    Useful after refining triage thresholds or prompts.
    After reset, the next ingest run will re-triage all of them.
    """
    c = cfg.get()
    cp = Checkpoint(Path(c["paths"]["checkpoint"]))
    n = cp.reset_trivial()
    if n:
        console.print(f"[green]Reset {n} trivial/flagged conversation(s) — they will be re-triaged on next ingest.")
    else:
        console.print("[yellow]No trivial/flagged conversations in checkpoint.")


@cli.command("reset")
@click.option("--all", "reset_all", is_flag=True, default=False,
              help="Reset ALL written conversations back to triage_done (re-summarize everything).")
def reset(reset_all: bool) -> None:
    """
    Reset written conversations back to triage_done so they get re-summarized.

    Use this when you change the summarization prompt or model and want to
    regenerate wiki pages without re-running triage.

    By default does nothing — you must pass --all to reset all written conversations.
    """
    c = cfg.get()
    cp = Checkpoint(Path(c["paths"]["checkpoint"]))

    if not reset_all:
        console.print("[yellow]No action taken. Pass --all to reset all written conversations.")
        console.print("[dim]Example: wiki reset --all")
        return

    n = cp.reset_written()
    if n:
        console.print(f"[green]Reset {n} written conversation(s) to triage_done — they will be re-summarized on next ingest.")
    else:
        console.print("[yellow]No written conversations in checkpoint.")


# ---------------------------------------------------------------------------
# triage (preview pass)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("export_dir", default=None, required=False)
@click.option("--limit", default=50, help="Max conversations to triage for preview")
def triage(export_dir: str | None, limit: int) -> None:
    """Triage-only pass — preview classifications before full ingest."""
    c = cfg.get()
    export_path = export_dir or c["paths"]["chatgpt_export"]
    client = OllamaClient(c["ollama"]["base_url"], timeout=c["ollama"]["timeout"])
    triage_model = c["ollama"]["triage_model"]

    counts: dict[str, int] = {"trivial": 0, "substantive": 0, "deep": 0}
    seen = 0

    with _make_progress() as progress:
        task = progress.add_task("Triaging (preview)...", total=limit)
        for conv in parse_export(export_path):
            if seen >= limit:
                break
            result = triage_conversation(
                conv,
                client,
                triage_model,
                head_chars=c["ollama"]["triage_context_chars"],
                min_messages=c["triage"]["min_messages"],
                min_chars=c["triage"]["min_chars"],
            )
            counts[result] = counts.get(result, 0) + 1
            progress.advance(task)
            seen += 1

    table = Table(title=f"Triage preview ({seen} conversations)")
    table.add_column("Class")
    table.add_column("Count")
    table.add_column("Est. % of all 3121")
    for cls, count in counts.items():
        pct = f"{100*count//seen}%" if seen else "-"
        table.add_row(cls, str(count), pct)
    console.print(table)

    non_trivial = counts.get("substantive", 0) + counts.get("deep", 0)
    est_total = int(non_trivial * 3121 / seen) if seen else 0
    console.print(f"\n[dim]Estimated substantive+deep conversations: ~{est_total}")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@cli.group()
def ingest() -> None:
    """Ingest sources into the wiki."""


@ingest.command("chatgpt")
@click.argument("export_dir", default=None, required=False)
@click.option("--triage-mode", default="llm",
              type=click.Choice(["llm", "programmatic", "none"]),
              help=(
                  "llm: 3B model classifies (default). "
                  "programmatic: rules only, no LLM — flags short/empty, sends rest to 14B. "
                  "none: skip triage entirely, summarize everything."
              ))
@click.option("--limit", default=0, help="Limit number of conversations (0 = all)")
@click.option("--reindex-interval", default=100, help="Regenerate index every N summarized conversations")
@click.option("--extra-url", "extra_urls_cli", multiple=True,
              help="Extra Ollama endpoint(s) for parallel summarization. Overrides config extra_ollama_urls.")
@click.option("--interleaved", is_flag=True, hidden=True,
              help="Old single-pass mode. Slower — switches model every conversation.")
def ingest_chatgpt(
    export_dir: str | None,
    triage_mode: str,
    limit: int,
    reindex_interval: int,
    extra_urls_cli: tuple,
    interleaved: bool,
) -> None:
    """
    Ingest ChatGPT export directory into the wiki.

    Runs in two passes by default:
      Pass 1 — triage all conversations (3B model stays loaded throughout)
      Pass 2 — summarize non-trivial conversations (14B model stays loaded throughout)

    This avoids model switching every conversation, which causes repeated
    load/unload cycles (~10-30s each for 14B). Both passes are resumable.
    """
    c = cfg.get()
    export_path = export_dir or c["paths"]["chatgpt_export"]
    vault = Path(c["paths"]["vault"])
    vault.mkdir(parents=True, exist_ok=True)

    base_url = c["ollama"]["base_url"]
    extra_urls = list(extra_urls_cli) if extra_urls_cli else c["ollama"].get("extra_ollama_urls", [])
    timeout = c["ollama"]["timeout"]

    primary = OllamaClient(base_url, timeout=timeout)
    console.print(f"[dim]Connecting to Ollama at {base_url}...")
    if not primary.is_available():
        console.print("[red]Ollama not reachable. Start with: ollama serve")
        sys.exit(1)
    console.print("[green]Ollama online[/]")

    clients = [primary]
    for url in extra_urls:
        cl = OllamaClient(url, timeout=5.0)
        if cl.is_available():
            clients.append(OllamaClient(url, timeout=timeout))
            console.print(f"[green]Extra endpoint online:[/] {url}")
        else:
            console.print(f"[yellow]Extra endpoint unreachable, skipping:[/] {url}")

    # Load graphify graph (entity normalization + edge enrichment)
    graph_path = Path(c["paths"]["graphify_graph"])
    console.print(f"Loading graphify graph from {graph_path}...")
    graph = load_graph(graph_path)
    console.print(f"[dim]Graph: {len(graph.nodes)} nodes, {sum(len(v) for v in graph.edges.values())//2} edges, {len(graph.communities)} communities")

    cp = Checkpoint(Path(c["paths"]["checkpoint"]))

    triage_model = c["ollama"]["triage_model"]
    summary_model = c["ollama"]["summary_model"]
    num_ctx = c["ollama"]["num_ctx"]
    triage_chars = c["ollama"]["triage_context_chars"]
    summary_chars = c["ollama"]["summary_context_chars"]
    mapreduce_threshold = c["ollama"]["mapreduce_threshold"]
    chunk_size = c["ollama"]["mapreduce_chunk_size"]
    chunk_overlap = c["ollama"]["mapreduce_chunk_overlap"]
    min_messages = c["triage"]["min_messages"]
    min_chars_cfg = c["triage"]["min_chars"]
    skip_trivial = c["triage"]["skip_trivial"]

    mode_label = f"{'interleaved' if interleaved else 'two-pass'} | triage={triage_mode}"
    append_log(vault, f"ingest start | source=chatgpt | export={export_path} | {mode_label}")
    console.print(f"[bold]Starting ingest[/] — {'interleaved' if interleaved else '[green]two-pass (fast)[/]'}")
    console.print(f"  Triage mode:   [cyan]{triage_mode}[/]")
    if triage_mode == "llm":
        console.print(f"  Triage model:  {triage_model}")
    console.print(f"  Summary model: {summary_model}  (num_ctx={num_ctx})")
    console.print(f"  Vault: {vault}")
    console.print()

    counts = {"flagged": 0, "trivial": 0, "substantive": 0, "deep": 0, "error": 0, "skipped": 0}
    start = time.time()

    if interleaved:
        _run_interleaved(
            export_path, vault, clients, cp, graph,
            triage_model, summary_model, num_ctx,
            triage_chars, summary_chars, mapreduce_threshold,
            chunk_size, chunk_overlap, min_messages, min_chars_cfg,
            triage_mode, skip_trivial, limit, reindex_interval, counts,
        )
    else:
        _run_two_pass(
            export_path, vault, clients, cp, graph,
            triage_model, summary_model, num_ctx,
            triage_chars, summary_chars, mapreduce_threshold,
            chunk_size, chunk_overlap, min_messages, min_chars_cfg,
            triage_mode, skip_trivial, limit, reindex_interval, counts,
        )

    cp.save()
    regenerate_index(vault)

    elapsed = time.time() - start
    append_log(vault, (
        f"ingest done | chatgpt | processed={counts['substantive']+counts['deep']} | "
        f"trivial={counts['trivial']} | substantive={counts['substantive']} | "
        f"deep={counts['deep']} | errors={counts['error']} | elapsed={elapsed:.0f}s"
    ))

    table = Table(title="Ingest Complete")
    table.add_column("Stat")
    table.add_column("Value")
    total_written = counts["substantive"] + counts["deep"]
    table.add_row("Wiki pages written", str(total_written))
    table.add_row("Trivial (skipped)", str(counts["trivial"]))
    table.add_row("Substantive", str(counts["substantive"]))
    table.add_row("Deep", str(counts["deep"]))
    table.add_row("Errors", str(counts["error"]))
    table.add_row("Time", f"{elapsed:.0f}s ({elapsed/3600:.1f}h)")
    console.print(table)
    console.print(f"[green]Vault: {vault}")
    console.print(f"[dim]Open in Obsidian: \\\\\\\\wsl$\\Ubuntu{vault}")


# ---------------------------------------------------------------------------
# Two-pass and interleaved implementations
# ---------------------------------------------------------------------------

def _run_two_pass(
    export_path, vault, clients, cp, graph,
    triage_model, summary_model, num_ctx,
    triage_chars, summary_chars, mapreduce_threshold,
    chunk_size, chunk_overlap, min_messages, min_chars_cfg,
    triage_mode, skip_trivial, limit, reindex_interval, counts,
):
    """
    Two-pass ingest:
    Pass 1: triage all conversations (model stays hot, no switching)
    Pass 2: summarize + write non-flagged conversations (14B stays hot)

    triage_mode:
      "llm"          — programmatic pre-filter + 3B LLM classification
      "programmatic" — rules only (min_messages / min_chars), no LLM
      "none"         — skip pass 1 entirely, send everything to 14B
    """
    # ── Pass 1: Triage ──────────────────────────────────────────────────────
    if triage_mode != "none":
        mode_label = "3B model" if triage_mode == "llm" else "rules only, no LLM"
        console.print(f"[bold cyan]Pass 1/2 — Triage[/] ({mode_label})")

        if triage_mode == "llm":
            console.print(f"[dim]Warming up {triage_model}...")
            clients[0].warmup(triage_model, num_ctx=4096)

        t1_new = 0
        with _make_progress() as progress:
            task = progress.add_task("Triaging...", total=None)

            for conv in parse_export(export_path):
                if limit and t1_new >= limit:
                    break
                progress.update(task, description=f"[triage {t1_new}] {conv.title[:45]}")

                if not cp.needs_processing(conv.id):
                    progress.advance(task)
                    continue

                if triage_mode == "llm":
                    result = triage_conversation(
                        conv, clients[0], triage_model,
                        head_chars=triage_chars,
                        min_messages=min_messages,
                        min_chars=min_chars_cfg,
                    )
                    if result == "flagged" or (result == "trivial" and skip_trivial):
                        cp.mark_trivial(conv.id)
                        counts["trivial"] += 1
                    else:
                        cp.mark_triage_done(conv.id, result)

                else:  # programmatic
                    flagged = triage_programmatic(conv, min_messages, min_chars_cfg)
                    if flagged:
                        reason = triage_programmatic_reason(conv, min_messages, min_chars_cfg)
                        cp.mark_flagged(conv.id, reason)
                        counts["flagged"] += 1
                    else:
                        cp.mark_triage_done(conv.id, "substantive")

                t1_new += 1
                progress.advance(task)

        cp.save()
        triage_stats = cp.stats()
        flagged = triage_stats.get("flagged", 0)
        trivial = triage_stats.get("trivial", 0)
        console.print(f"[dim]Triage done: flagged={flagged}, trivial={trivial}, to summarize={triage_stats.get('triage_done', 0)}")

    # ── Pass 2: Summarize + Write ────────────────────────────────────────────
    n_workers = len(clients)
    console.print(f"\n[bold cyan]Pass 2/2 — Summarize & Write[/] ({n_workers} worker{'s' if n_workers > 1 else ''})")
    console.print(f"[dim]Warming up {summary_model} on {n_workers} endpoint(s) (num_ctx={num_ctx})...")
    with ThreadPoolExecutor(max_workers=n_workers) as warmup_pool:
        for f in [warmup_pool.submit(cl.warmup, summary_model, num_ctx) for cl in clients]:
            f.result()

    # Thread-safe client pool — each worker grabs a client, uses it, returns it
    _client_pool: _queue.Queue = _queue.Queue()
    for cl in clients:
        _client_pool.put(cl)

    def _summarize_one(conv, triage_result):
        cl = _client_pool.get()
        try:
            return summarize_conversation(
                conv, triage_result, cl, summary_model,
                max_chars=summary_chars,
                mapreduce_threshold=mapreduce_threshold,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                num_ctx=num_ctx,
            ), None
        except Exception as e:
            return None, e
        finally:
            _client_pool.put(cl)

    # Collect pending conversations (skip done/trivial)
    pending = []
    for conv in parse_export(export_path):
        if limit and len(pending) >= limit:
            break
        if cp.is_done(conv.id):
            continue
        if cp.is_trivial(conv.id):
            counts["trivial"] += 1
            continue
        cp_entry = cp._data.get(conv.id, {})
        triage_result = cp_entry.get("triage", "substantive") if triage_mode != "none" else "substantive"
        pending.append((conv, triage_result))

    already_done = cp.stats().get("written", 0)
    console.print(f"[dim]Checkpoint: {already_done} already written, {len(pending)} remaining")

    # Signal handler — sets a flag instead of raising KeyboardInterrupt,
    # so Click cannot intercept it. Second Ctrl+C restores default (force-kills).
    _shutdown = _threading.Event()
    _orig_sigint = _signal.getsignal(_signal.SIGINT)

    def _on_sigint(sig, frame):
        if not _shutdown.is_set():
            _shutdown.set()
            _signal.signal(_signal.SIGINT, _orig_sigint)  # second ^C = hard exit

    _signal.signal(_signal.SIGINT, _on_sigint)

    written = 0
    try:
        with ThreadPoolExecutor(max_workers=n_workers) as executor, _make_progress() as progress:
            task = progress.add_task("Summarizing...", total=len(pending))
            future_map = {executor.submit(_summarize_one, conv, tr): (conv, tr) for conv, tr in pending}
            remaining = set(future_map.keys())

            while remaining:
                done, remaining = _futures_wait(remaining, timeout=0.5, return_when=FIRST_COMPLETED)

                if _shutdown.is_set():
                    # Cancel everything not yet started
                    for f in remaining:
                        f.cancel()
                    # Show what we're still waiting for
                    in_flight = [future_map[f][0].title for f in remaining if not f.cancelled()]
                    if in_flight:
                        progress.update(task, description=f"[yellow]Stopping — finishing: {in_flight[0][:40]}...")
                    elif not done:
                        break  # all cancelled, nothing left to drain

                for future in done:
                    if future.cancelled() or (_shutdown.is_set() and not future.done()):
                        progress.advance(task)
                        continue

                    conv, triage_result = future_map[future]
                    summary, error = future.result()

                    if _shutdown.is_set():
                        # Drain result but don't write — keep conv in triage_done for resume
                        progress.advance(task)
                        continue

                    progress.update(task, description=f"[write {written}] {conv.title[:45]}")

                    if error:
                        console.print(f"\n[red]Error on {conv.title[:40]}: {error}")
                        counts["error"] += 1
                    else:
                        page_path = write_conversation_page(conv, summary, triage_result, vault, graph)
                        wiki_rel = str(page_path.relative_to(vault).with_suffix(""))
                        for entity in summary.entities:
                            update_entity_page(entity["name"], entity["type"], conv, wiki_rel, vault, graph)
                        cp.mark_written(conv.id, wiki_rel)
                        counts[triage_result] = counts.get(triage_result, 0) + 1
                        written += 1

                    progress.advance(task)

                    if reindex_interval and written % reindex_interval == 0:
                        regenerate_index(vault)

    finally:
        _signal.signal(_signal.SIGINT, _orig_sigint)

    if _shutdown.is_set():
        cp.save()
        console.print(f"[yellow]Checkpoint saved — {written} written this session. Resume with same command.")
        sys.exit(0)


def _run_interleaved(
    export_path, vault, clients, cp, graph,
    triage_model, summary_model, num_ctx,
    triage_chars, summary_chars, mapreduce_threshold,
    chunk_size, chunk_overlap, min_messages, min_chars_cfg,
    triage_mode, skip_trivial, limit, reindex_interval, counts,
):
    """Original single-pass: triage then summarize per conversation. Switches model every conversation."""
    processed = 0
    with _make_progress() as progress:
        task = progress.add_task("Ingesting...", total=None)

        for conv in parse_export(export_path):
            if limit and processed >= limit:
                break
            progress.update(task, description=f"[{processed}] {conv.title[:50]}")

            if cp.is_done(conv.id):
                counts["skipped"] += 1
                continue
            if cp.is_trivial(conv.id):
                counts["trivial"] += 1
                counts["skipped"] += 1
                continue

            try:
                if triage_mode == "none":
                    triage_result = "substantive"
                elif triage_mode == "programmatic":
                    flagged = triage_programmatic(conv, min_messages, min_chars_cfg)
                    if flagged:
                        reason = triage_programmatic_reason(conv, min_messages, min_chars_cfg)
                        cp.mark_flagged(conv.id, reason)
                        counts["flagged"] += 1
                        processed += 1
                        progress.advance(task)
                        continue
                    triage_result = "substantive"
                else:  # llm
                    triage_result = triage_conversation(
                        conv, clients[0], triage_model,
                        head_chars=triage_chars,
                        min_messages=min_messages,
                        min_chars=min_chars_cfg,
                    )
                    if triage_result == "flagged":
                        cp.mark_flagged(conv.id)
                        counts["flagged"] += 1
                        processed += 1
                        progress.advance(task)
                        continue
                    if triage_result == "trivial" and skip_trivial:
                        cp.mark_trivial(conv.id)
                        counts["trivial"] += 1
                        processed += 1
                        progress.advance(task)
                        continue

                cp.mark_triage_done(conv.id, triage_result)
                summary = summarize_conversation(
                    conv, triage_result, clients[0], summary_model,
                    max_chars=summary_chars,
                    mapreduce_threshold=mapreduce_threshold,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    num_ctx=num_ctx,
                )
                page_path = write_conversation_page(conv, summary, triage_result, vault, graph)
                wiki_rel = str(page_path.relative_to(vault).with_suffix(""))
                for entity in summary.entities:
                    update_entity_page(entity["name"], entity["type"], conv, wiki_rel, vault, graph)
                cp.mark_written(conv.id, wiki_rel)
                counts[triage_result] = counts.get(triage_result, 0) + 1

            except Exception as e:
                console.print(f"\n[red]Error on {conv.title[:40]}: {e}")
                counts["error"] += 1

            processed += 1
            progress.advance(task)
            if reindex_interval and processed % reindex_interval == 0:
                regenerate_index(vault)


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--limit", default=0, help="Max communities to synthesize (0 = all)")
@click.option("--community", "community_slug", default=None,
              help="Synthesize only this community slug (see --list for slugs)")
@click.option("--list", "list_only", is_flag=True,
              help="List communities and exit — no LLM calls")
@click.option("--reset", "reset_slug", default=None, metavar="SLUG",
              help="Reset a community for re-synthesis. Pass 'all' to reset everything.")
def synthesize(limit: int, community_slug: str | None, list_only: bool, reset_slug: str | None) -> None:
    """
    Pass 3 — synthesize community knowledge into topic pages.

    Reads named communities from index.md, loads conversation summaries,
    and writes one synthesized topic page per community to vault/wiki/.
    Resumable — skips already-done communities.
    """
    c = cfg.get()
    vault = Path(c["paths"]["vault"])
    index_path = vault / "index.md"

    if not index_path.exists():
        console.print("[red]index.md not found. Run 'wiki reindex' first.")
        sys.exit(1)

    checkpoint_path = Path(c["paths"]["checkpoint"]).parent / "synthesis_checkpoint.json"
    cp = SynthesisCheckpoint(checkpoint_path)

    # ── Reset mode ──────────────────────────────────────────────────────────
    if reset_slug:
        target = None if reset_slug == "all" else reset_slug
        n = cp.reset(target)
        console.print(f"[green]Reset {n} entr{'y' if n == 1 else 'ies'}.")
        return

    console.print(f"[dim]Parsing communities from {index_path}...")
    communities = parse_communities(index_path)
    console.print(f"[dim]Found {len(communities)} named communities with conversation pages.")

    # ── List mode ───────────────────────────────────────────────────────────
    if list_only:
        table = Table(title=f"Communities ({len(communities)})")
        table.add_column("Slug")
        table.add_column("Name")
        table.add_column("Convs")
        table.add_column("Status")
        for comm in communities:
            status = "[green]done" if cp.is_done(comm.slug) else "[dim]pending"
            table.add_row(comm.slug, comm.name, str(comm.count), status)
        console.print(table)
        return

    # ── Filter ──────────────────────────────────────────────────────────────
    if community_slug:
        targets = [comm for comm in communities if comm.slug == community_slug]
        if not targets:
            console.print(f"[red]Community slug not found: {community_slug}")
            console.print("[dim]Run 'wiki synthesize --list' to see available slugs.")
            sys.exit(1)
    else:
        targets = [comm for comm in communities if not cp.is_done(comm.slug)]
        if limit:
            targets = targets[:limit]

    already_done = len(communities) - len([comm for comm in communities if not cp.is_done(comm.slug)])
    console.print(f"[dim]Checkpoint: {already_done} done, {len(targets)} remaining")

    if not targets:
        console.print("[green]All communities already synthesized.")
        return

    # ── LLM setup ───────────────────────────────────────────────────────────
    primary = OllamaClient(c["ollama"]["base_url"], timeout=c["ollama"]["timeout"])
    if not primary.is_available():
        console.print("[red]Ollama not reachable. Start with: ollama serve")
        sys.exit(1)

    model = c["ollama"]["summary_model"]
    num_ctx = c["ollama"]["num_ctx"]

    console.print(f"[dim]Warming up {model} (num_ctx={num_ctx})...")
    primary.warmup(model, num_ctx=num_ctx)

    from datetime import date as _date
    today = str(_date.today())

    # ── Synthesis loop ───────────────────────────────────────────────────────
    done = 0
    errors = 0

    with _make_progress() as progress:
        task = progress.add_task("Synthesizing...", total=len(targets))

        for comm in targets:
            progress.update(task, description=f"[{done+1}/{len(targets)}] {comm.name[:50]}")

            try:
                items = load_community_summaries(comm, vault)
                if not items:
                    console.print(f"\n[yellow]No pages found for: {comm.name} — skipping")
                    cp.mark_error(comm.slug, "no conversation pages found")
                    errors += 1
                    progress.advance(task)
                    continue

                content = synthesize_community(comm, items, primary, model, num_ctx=num_ctx)
                write_synthesis_page(comm, content, vault, today)
                cp.mark_done(comm.slug)
                done += 1

            except Exception as e:
                console.print(f"\n[red]Error on '{comm.name}': {e}")
                cp.mark_error(comm.slug, str(e))
                errors += 1

            progress.advance(task)

    table = Table(title="Synthesize Complete")
    table.add_column("Stat")
    table.add_column("Value")
    table.add_row("Topic pages written", str(done))
    table.add_row("Errors", str(errors))
    table.add_row("Output", str(vault / "wiki"))
    console.print(table)


if __name__ == "__main__":
    cli()
