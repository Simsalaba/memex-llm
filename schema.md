# LLM Wiki — Schema & Conventions

## Philosophy

This wiki compiles knowledge distilled from all sources the user has touched. It is maintained by LLMs (primarily Ollama local models) with human-strategic oversight. Every page should be useful to both the user and an LLM reading it for context.

## Page Types

### Conversation Pages (`vault/conversations/YYYY-MM/title-slug.md`)
One page per ingested conversation. Contains distilled summary, key points, and entity links. Source attribution in frontmatter. Organized by month.

### Entity Pages (`vault/entities/entity-slug.md`)
One page per named entity (tool, person, project, concept, service). Aggregates across all conversations. Grows over time as more sources mention the entity.

### Topic Pages (`vault/topics/topic-slug.md`)
High-level synthesis pages corresponding to graphify community clusters. Written by LLM, cross-link many entity and conversation pages. Represent the user's areas of interest.

### Index (`vault/index.md`)
Auto-generated catalog. Lists all pages by category with one-line summaries. Regenerated after each batch.

### Log (`vault/log.md`)
Append-only. Records ingest events, model runs, and maintenance operations.

## Entity Taxonomy

```
tools:       software, frameworks, CLIs, libraries
services:    cloud services, APIs, platforms
projects:    specific named projects (user's or external)
concepts:    abstract ideas, techniques, methodologies
people:      named individuals
orgs:        companies, teams, communities
hardware:    physical devices, components
topics:      broad subject areas (typically = graphify community)
```

## Frontmatter Standards

### Conversation page
```yaml
---
source: chatgpt          # chatgpt | slack | web | repo | claude
conversation_id: "uuid"
date: YYYY-MM-DD
model: gpt-4             # LLM used in that conversation
triage: substantive      # trivial | substantive | deep
community: "DevOps Career & Core Tools"  # graphify community
tags: [tag1, tag2]
---
```

### Entity page
```yaml
---
type: entity
category: tool           # tool | service | project | concept | person | org | hardware | topic
first_seen: YYYY-MM-DD
mention_count: N
community: "..."
graphify_node_id: "topic_kubernetes"  # original graphify node ID if mapped
---
```

## Wikilink Conventions

- Always use `[[Page Title]]` format for Obsidian wikilinks
- Entity links use the entity's canonical name (title-cased)
- Community links: `[[topics/community-slug|Community Name]]`
- Conversation links always include month path: `[[conversations/2024-01/title]]`

## Canonicalization

Entity names are normalized against graphify's existing node list. When adding a new entity:
1. Fuzzy-match against existing graphify nodes
2. If match score ≥ 85, use graphify's canonical label
3. Otherwise create new entity with slugified name

## Content Guidelines

- Summaries: 2-4 sentences, factual, no filler
- Key points: bullet list, each point self-contained
- Entity summaries: describe in context of THIS user's usage, not generic definition
- No timestamps in content (only frontmatter) — content stays evergreen
