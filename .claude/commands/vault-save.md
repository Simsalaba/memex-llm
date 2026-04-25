---
description: Save valuable session knowledge to the Obsidian vault — decisions, discoveries, handoff state
---

Review the current conversation and save anything worth keeping permanently to the Obsidian vault at `/mnt/c/Users/Koroshiya/llm-wiki-vault`.

## Step 1 — Identify what's worth saving

Scan the conversation for:
- **Decisions** — architectural choices, why X was built this way, tradeoffs considered
- **Discoveries** — gotchas, things that don't work and why, non-obvious findings
- **Handoff state** — current state of work, what's done, what's next, blockers
- **Designs** — schemas, structures, approaches agreed on

Skip: things already in code, obvious facts, transient debugging steps.

If nothing meaningful was produced, say so and stop.

## Step 2 — Propose before writing

Present a draft note to the user:

```
Title: YYYY-MM-DD-topic-slug
Type: decision | discovery | handoff | design
---
[proposed content]
```

Ask: "Write this to the vault?" — wait for confirmation before proceeding.

## Step 3 — Write the note

On confirmation, write to:
```
/mnt/c/Users/Koroshiya/llm-wiki-vault/notes/YYYY-MM-DD-topic-slug.md
```

Use this frontmatter:
```yaml
---
date: YYYY-MM-DD
type: decision | discovery | handoff | design
project: [project name if clear]
tags: [relevant tags]
---
```

Then the content — concise, factual, written for future-you who has forgotten this session:
- **What**: the decision/discovery/state
- **Why**: the reasoning or cause
- **Watch out**: anything that will bite you later (for discoveries)
- **Next**: what to do next (for handoffs only)

No fluff. No "in this session we explored...". Just the facts.

## Notes

- Create `notes/` folder if it doesn't exist
- Use kebab-case for filenames
- If multiple distinct things are worth saving, write multiple files — don't cram everything into one
- Today's date: use the currentDate from system context if available
