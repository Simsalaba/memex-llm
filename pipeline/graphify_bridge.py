"""
Graphify bridge — loads an existing graph.json produced by /graphify and exposes:

  - Entity normalization: "kubernetes" → canonical label from graphify nodes
  - Edge lookup: given an entity, return its graph neighbors
  - Community lookup: given an entity, return its community name

This is loaded once at pipeline startup and passed to the wiki writer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process as fuzz_process


@dataclass
class GraphData:
    # node_id → display label
    nodes: dict[str, str] = field(default_factory=dict)
    # node_id → community name
    node_community: dict[str, str] = field(default_factory=dict)
    # node_id → set of neighbor node_ids
    edges: dict[str, set[str]] = field(default_factory=dict)
    # community_id → list of node_ids
    communities: dict[str, list[str]] = field(default_factory=dict)
    # label (lowercase) → node_id (for fast fuzzy matching)
    label_index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Build lowercase label index for fuzzy matching
        self.label_index = {
            label.lower(): node_id
            for node_id, label in self.nodes.items()
        }


def load_graph(path: str | Path) -> GraphData:
    """
    Parse a graphify graph.json and return a GraphData object.

    Actual graph.json structure:
    {
      "nodes": [{"id": "topic_jenkins", "label": "Jenkins CI/CD", "norm_label": "jenkins ci/cd",
                 "community": 2 (int), ...}],
      "links": [{"source": "topic_jenkins", "target": "topic_gerrit", ...}],
      "hyperedges": [{"id": "cluster_...", "label": "...", "nodes": [...], ...}],
      "graph": {"hyperedges": [...]}   # same hyperedges, sometimes here too
    }
    """
    path = Path(path)
    if not path.exists():
        return GraphData()

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    gdata = GraphData()

    # Parse hyperedges first — community int ID → label mapping
    # Hyperedges appear at top-level and/or inside graph.hyperedges
    all_hyperedges = list(raw.get("hyperedges", []))
    all_hyperedges += raw.get("graph", {}).get("hyperedges", [])

    # Build: community_int_id → label (from hyperedge ordering or explicit community key)
    # Also build: node_id → community label (via hyperedge membership)
    hyperedge_node_community: dict[str, str] = {}
    for hedge in all_hyperedges:
        hedge_label = hedge.get("label", hedge.get("id", ""))
        hedge_id = hedge.get("id", "")
        members = hedge.get("nodes", [])
        gdata.communities[hedge_id] = members
        for node_id in members:
            if node_id not in hyperedge_node_community:
                hyperedge_node_community[node_id] = hedge_label

    # Parse nodes — use norm_label for fuzzy matching index if available
    for node in raw.get("nodes", []):
        node_id = node.get("id", "")
        if not node_id:
            continue
        label = node.get("label", "") or node_id
        norm_label = node.get("norm_label", label.lower())
        gdata.nodes[node_id] = label

        # Community: prefer hyperedge membership (has readable label)
        if node_id in hyperedge_node_community:
            gdata.node_community[node_id] = hyperedge_node_community[node_id]

    # Parse edges (links)
    for link in raw.get("links", []):
        src = link.get("source", "")
        tgt = link.get("target", "")
        if src and tgt:
            gdata.edges.setdefault(src, set()).add(tgt)
            gdata.edges.setdefault(tgt, set()).add(src)

    gdata.__post_init__()
    return gdata


def normalize_entity(name: str, graph: GraphData, threshold: int = 80) -> str:
    """
    Fuzzy-match 'name' against graphify node labels.
    Returns the canonical label if score >= threshold, else returns name as-is.
    """
    if not graph.label_index:
        return name

    name_lower = name.lower()

    # Exact match first
    if name_lower in graph.label_index:
        node_id = graph.label_index[name_lower]
        return graph.nodes[node_id]

    # Fuzzy match
    result = fuzz_process.extractOne(
        name_lower,
        graph.label_index.keys(),
        scorer=fuzz.partial_ratio,
        score_cutoff=threshold,
    )
    if result:
        matched_label_lower, score, _ = result
        node_id = graph.label_index[matched_label_lower]
        return graph.nodes[node_id]

    return name


def get_related(entity_name: str, graph: GraphData, max_relations: int = 10) -> list[str]:
    """
    Return neighbor entity labels for a given entity name.
    Used to add extra [[wikilinks]] beyond what Ollama extracted.
    """
    if not graph.label_index:
        return []

    name_lower = entity_name.lower()
    node_id: str | None = None

    # Find node_id for this entity
    if name_lower in graph.label_index:
        node_id = graph.label_index[name_lower]
    else:
        result = fuzz_process.extractOne(
            name_lower,
            graph.label_index.keys(),
            scorer=fuzz.partial_ratio,
            score_cutoff=80,
        )
        if result:
            matched, _score, _ = result
            node_id = graph.label_index[matched]

    if node_id is None:
        return []

    neighbors = graph.edges.get(node_id, set())
    return [graph.nodes[n] for n in neighbors if n in graph.nodes][:max_relations]


def get_community(entity_name: str, graph: GraphData) -> str:
    """Return the community name for a given entity, or empty string."""
    if not graph.label_index:
        return ""

    name_lower = entity_name.lower()
    node_id: str | None = None

    if name_lower in graph.label_index:
        node_id = graph.label_index[name_lower]
    else:
        result = fuzz_process.extractOne(
            name_lower,
            graph.label_index.keys(),
            scorer=fuzz.partial_ratio,
            score_cutoff=80,
        )
        if result:
            matched, _score, _ = result
            node_id = graph.label_index[matched]

    if node_id is None:
        return ""

    return graph.node_community.get(node_id, "")
