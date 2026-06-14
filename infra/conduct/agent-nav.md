---
type: agent-navigation-contract
canonical: true
applies_to: [claude, codex, gemini, hermes, dust]
created: 2026-06-13
source_of_truth: specs/sbap-schemas/agent_write.schema.json
---

# Agent Navigation & Write Contract — the one shared map

> **Single source of truth for how EVERY model navigates and writes to this brain** (Claude · Codex · Gemini · Hermes · Dust). Model-specific files hold model-specific notes; **navigation order and the SBAP write contract are defined HERE so they cannot drift apart.** If a per-model file disagrees with this doc, this doc wins.

## 1. Read order (context lookup) — identical for all agents

1. **`/graphify query "<topic>"`** — the knowledge graph (cheapest, freshest).
2. **`_brain_api/<endpoint>.json`** — pre-computed canonical answers: `canonical/<type>/<key>.json`, `account/<client>/brief.json`, `bid/_open.json`, `changes/since_<ts>.json`, `_agent_state/<self>/memory.json`.
3. **`Read` raw files** — only when 1 and 2 are empty, or the owner explicitly says "read the file".

Never crawl folders when an endpoint answers the question.

## 2. Write contract (SBAP v1.0) — identical for all agents

Write to **`00_Inbox/from-dust/<agent>/`** (agent name matches `^[a-z0-9-]+$` — hyphens, never underscores). Every written `.md` MUST carry these **8 required** frontmatter fields:

| field | meaning |
|---|---|
| `sbap_version` | `"1.0"` |
| `source_agent` | registered name from `_agent_state/_registry.json` |
| `source_run_id` | unique invocation id |
| `generated` | ISO-8601 datetime |
| `input_context_refs` | array of every `_brain_api/`/`_brain_index/` entry consulted |
| `output_type` | one value from the locked enum in the schema |
| `target_path` | where triage should file it (may be empty for review-only) |
| `confidence` | 0.0–1.0 |

- Triage **auto-promotes `confidence ≥ 0.85`**; holds the rest for review.
- **Last-write-wins is FORBIDDEN.** Conflicts become versioned files (`<target>.dust-<agent>-<ts>.md`); resolve via `/dust-resolve`.
- Adding a new `output_type` means editing BOTH the schema enum AND the triage allowlist (`build/tools/triage_dust_writes.py`) — they must stay in sync.

## 3. Relay baton — on any multi-step task

Read **`_relay/BATON.md` first**; keep it true while you work; hand off cleanly (self-contained, stamped footer, one line to `_relay/log.md`). Full protocol: `_relay/README.md`.

## 4. Behavior standard (Fable-5 grade)

How every model here reasons, sources, refuses, and handles being wrong — the shared CONDUCT contract: **[agent-behavior-standard.md](agent-behavior-standard.md)**. It governs conduct (honesty, search discipline, sourcing/IP, legal-financial caution, evenhandedness, wellbeing, refusals); style still defers to Brand DNA, persona voice, and caveman mode.

## 5. The non-negotiables

- The **vault IS the shared memory** — the markdown + JSON files here are the source of truth, not any model's private cache.
- **Plan before you execute.** Simplicity first · root causes · minimal blast radius.
- **Never auto-send anything external.** Drafts only — the owner reviews. HR Documents/ and LinkedIn/ are off-limits; route PII / client-confidential content through the confidentiality guard.
