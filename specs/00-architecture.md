# Spec 00 — Architecture: the agent-native vault

## Design laws (apply to every later spec)

1. **No theater.** Every pixel binds to real data. Features render honest empty states ("no deadlines set — add `deadline:` to your brief frontmatter") instead of inventing signal. One fake number destroys trust in all the real ones.
2. **The vault IS the shared memory.** Plain Markdown + JSON files are the source of truth. Agents, plugins, and pipelines read/write files — no databases, no servers. Sync (iCloud/OneDrive/Syncthing) is transport, not truth.
3. **Machine-readable surfaces.** Agents should query pre-computed JSON, not crawl folders. Generated views are disposable; committed state is sacred.
4. **Provenance everywhere.** Generated content carries frontmatter saying which agent/process made it, when, from what. Confidence-gate anything auto-promoted.
5. **Evidence or it didn't happen.** Health logs, build stamps, behavioral self-tests. (See spec 06 for why.)

## The four content layers

```
<Vault>/
├── ── HUMAN NAV (their daily driver folders — from interview) ──
│   ├── <Things>/            # their lifecycle items (deals/papers/cases) — one folder per item
│   │   └── <Item>/
│   │       ├── 00 - Brief.md        # frontmatter: stage, entity, deadline, value — THE canonical record
│   │       ├── 01 - Stakeholders.md
│   │       ├── 02 - Draft.md
│   │       └── 03 - Decision Log.md
│   ├── <People-or-Entities>/        # one folder per relationship; _brief.md w/ last_touch frontmatter
│   ├── Meetings/                    # dated notes: YYYY-MM-DD-<slug>.md — dates IN FILENAMES (mtimes lie under sync)
│   ├── Daily/                       # daily notes YYYY-MM-DD.md
│   ├── Inbox/                       # dump zone; agents write to Inbox/from-agents/<agent>/
│   └── Archive/                     # closed items, by year
├── ── MACHINE LAYER ──
│   ├── _brain_api/                  # generated JSON endpoints (gitignored) — see spec 05
│   ├── _agent_state/                # per-agent memory/stats (COMMITTED — survives disaster recovery)
│   └── _meta/                       # scripts, config, health logs, schemas
└── .obsidian/plugins/<plugin>/      # the command-center plugin (spec 01)
```

Adapt names to the user's vocabulary. If they have an existing vault: map, don't move — the machine layer and plugin attach to ANY folder taxonomy via configurable path prefixes.

## Frontmatter contracts (the load-bearing fields)

These exact fields power most features; establish them in Phase 1 and the dashboards light up automatically:

| File | Fields | Consumed by |
|---|---|---|
| `<Item>/00 - Brief.md` | `stage`, `entity`, `deadline` (YYYY-MM-DD), `value`, `status` | pipeline card, raid boss, radar, phantoms, launch control |
| `<Entity>/_brief.md` | `last_touch` (YYYY-MM-DD), `poi_snooze` | aggro radar, POV reticles |
| agent writes | `source_agent`, `generated`, `confidence` (0–1), `target_path` | triage gate, marble run |
| dated notes | date in FILENAME | last-contact inference (never trust mtime under cloud sync) |

## The agent-write protocol (SBAP-lite)

If the user runs writing agents (or wants the assistant to draft things):
- Agents write ONLY to `Inbox/from-agents/<agent-name>/` with the frontmatter contract above.
- A triage step (script or manual) promotes `confidence ≥ 0.85` writes to their `target_path`; holds the rest for review.
- Conflicts = versioned files (`<name>.agent-<ts>.md`). Last-write-wins is forbidden.
- Every agent gets `_agent_state/<agent>/` with `memory.json` (learnings) and `writes.jsonl` (audit trail: ts, target, confidence, action).

## Multi-model relay (optional but cheap)

A `_relay/BATON.md` at vault root: current task state, done, blockers, next concrete action — written for a competent stranger. Any agent (Claude/Codex/local) reads it first on multi-step work and updates it before stopping. Plus `_relay/ISSUES.md`: the codified backlog — no issue lives only in chat.

## Platform notes

- Everything here is plain files — works on macOS/Windows/Linux identically.
- The refresh loop (spec 05) and voice pipeline (spec 02) are the only OS-specific parts.
