# CLAUDE.md — Your Second Brain (base standard)

This vault is **agent-native**: it exposes machine-readable surfaces (`_brain_api/`, `_agent_state/`, `_brain_index/`) so AI agents query pre-computed answers instead of crawling files. You (the AI reading this) operate under this contract.

## Rule 0 — Model routing
Use your strongest model for PLANNING only; execute with the fast/cheap tier. Never silently switch up mid-task.

## Rule 1 — Lookup order (cheapest first)
1. `_brain_api/<endpoint>.json` — pre-computed answers (bids, promises, contradictions, account briefs)
2. The knowledge graph / search index, if installed
3. Raw `Read` of vault files — last resort, or when the user says "read the file"

## Rule 2 — The vault IS the shared agent memory
Every agent writes to `00_Inbox/from-dust/<agent>/` with SBAP frontmatter (`source_agent`, `source_run_id`, `generated`, `output_type`, `target_path`, `confidence`). Triage (`build/tools/triage_dust_writes.py`) promotes confidence ≥ 0.85, holds the rest. Conflicts become versioned files — last-write-wins is FORBIDDEN.

**Canonical navigation & write contract (all models):** [`99_Meta/agent-nav.md`](99_Meta/agent-nav.md) — the single source of truth for how every model (Claude, Codex, Gemini, Hermes, Dust) navigates and writes to this brain. If any per-model instruction file disagrees with agent-nav.md, agent-nav.md wins.

**Shared conduct standard (Fable-5 grade):** [`99_Meta/agent-behavior-standard.md`](99_Meta/agent-behavior-standard.md) — how every agent sources, reasons, refuses, and handles being wrong. Governs honesty, search discipline, sourcing/IP, legal-financial caution, evenhandedness, wellbeing, and refusals. Style defers to your brand DNA; conduct binds on every turn. Inline core: see `99_Meta/conduct-core.md`.

## Rule 3 — Verification before done
Never claim a feature/fix works because "the diff looks right". Drive it, observe the effect, then say done. The plugin ships demo commands ("UX: … demo") and a self-test for exactly this.

## Rule 4 — No theater
Every visual binds to real data. Missing data → honest empty state. Never fabricate motion, urgency, or numbers.

## Rule 5 — Confidentiality
Nothing leaves this machine without an explicit confidentiality check. `Meetings/Confidential/` is gitignored, index-excluded, and off-limits to background agents — keep it that way.

## Daily rhythm
1. SessionStart hook prints the brief (freshness, open bids, promises pulse, stale agents)
2. Work in the relevant project folder; capture decisions in notes
3. The hourly refresh loop (`build/tools/` via `99_Meta/brain-refresh.sh`) keeps `_brain_api/` fresh

## The infrastructure map
| Piece | Where |
|---|---|
| Pipeline tools (index, api, triage, ledger, tape…) | `build/tools/` |
| Hooks (session brief, refresh loop) | `99_Meta/` |
| Machine layer (generated) | `_brain_api/`, `_brain_index/` — never hand-edit |
| Agent memory (committed) | `_agent_state/` |
| Cross-model baton | `_relay/` (read `BATON.md` first on any multi-step task) |

## Relay
On any multi-step task: read `_relay/BATON.md` first, keep it true while working, hand off cleanly (see `_relay/README.md`).

---
*Personalize the content of this vault freely. Keep this contract and the structure — they are the standard that makes shared tooling, specs, and fixes drop in without translation.*
*Built on Tony Second Brain by Tony Walteur — https://www.linkedin.com/in/tony-walteur-7067b81a2/*
