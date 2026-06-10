# Spec 00 — Architecture: the agent-native vault

## Design laws (apply to every later spec)

1. **No theater.** Every pixel binds to real data. Features render honest empty states ("no deadlines set — add `deadline:` to your brief frontmatter") instead of inventing signal. One fake number destroys trust in all the real ones.
2. **The vault IS the shared memory.** Plain Markdown + JSON files are the source of truth. Agents, plugins, and pipelines read/write files — no databases, no servers. Sync (iCloud/OneDrive/Syncthing) is transport, not truth.
3. **Machine-readable surfaces.** Agents should query pre-computed JSON, not crawl folders. Generated views are disposable; committed state is sacred.
4. **Provenance everywhere.** Generated content carries frontmatter saying which agent/process made it, when, from what. Confidence-gate anything auto-promoted.
5. **Evidence or it didn't happen.** Health logs, build stamps, behavioral self-tests. (See spec 06 for why.)

## The vault structure (build this exactly)

This is the original system's real, production-tested layout — **four content layers + daily-driver navigation**. Build the structure as-is; the *content* is what gets personalized. If the user's world isn't deal/bid-shaped, rename only the domain folders (`RFPs/` → their pipeline word, `Clients/` → their entity word) and keep everything else verbatim.

```
<Vault>/
├── CLAUDE.md / AGENTS.md        # the operating contract their agent reads every session
│
├── ── DAILY-DRIVER NAVIGATION ──
├── Meetings/                    # transcripts, prep, recaps — dated filenames YYYY-MM-DD-<slug>.md
├── Important/                   # priority queue — escalations, decisions-pending
├── Clients/                     # one folder per relationship, each with _brief.md (last_touch frontmatter)
├── RFPs/                        # the active pipeline — one folder per live item
├── People/                      # decision-makers, partners, peers
├── Outbound/                    # send queue — drafts awaiting human review (nothing auto-sends)
├── Reading/                     # incoming knowledge stream
├── Use Cases/                   # proven solutions, reference architectures, win stories
├── Preferences/                 # dont.md (rules for the agent), Lessons.md (solved-problems register)
│
├── ── RAW LAYER ──
├── 00_Inbox/                    # dump anything; agents write ONLY to 00_Inbox/from-agents/<agent>/
│
├── ── WIKI LAYER (LLM-maintained) ──
├── _wiki/                       # auto-organized distilled knowledge: _master-index.md, per-entity topics,
│                                #   patterns/, competitive/, failure-modes/, methodology/
│
├── ── OUTPUT LAYER (PARA) ──
├── 01_Projects/                 # active work items (one folder each: 00 - Brief.md, 01 - Stakeholders.md,
│                                #   02 - Draft.md, 03 - Decision Log.md) — keep a _template/ folder
├── 02_Areas/                    # ongoing areas — Accounts/, Daily/ (daily notes), Pipeline.md dashboard
├── 03_Resources/                # reusable reference — standards, templates, prompt libraries, intel
├── 04_Archives/                 # closed items, by year
├── 99_Meta/                     # vault config: scripts, schemas, config lists, health logs, CHANGELOG
│
├── ── MACHINE LAYER ──
├── _brain_index/                # generated search/index artifacts (gitignored)
├── _brain_api/                  # generated JSON query endpoints (gitignored) — see spec 05
├── _agent_state/                # per-agent memory + stats (COMMITTED — persistent memory survives
│                                #   disaster recovery): <agent>/memory.json, writes.jsonl, stats
├── _relay/                      # multi-model handoff: BATON.md (live task state) + ISSUES.md (attack queue)
│
└── .obsidian/plugins/<name>/    # the command-center plugin (spec 01)
```

Conventions that make it work:
- `01_Projects/` contents and the pipeline folder are the same things viewed two ways — items live in one place, dashboards read frontmatter.
- A `_template/` item folder to copy for every new item.
- Vault is a **git repo**: all human layers + `99_Meta/` + `_agent_state/` committed; `_brain_index/`, `_brain_api/`, build outputs gitignored (regenerable).
- Heavy pipeline code lives OUTSIDE the vault (e.g. `~/brain-build/`) — the vault holds a symlink or absolute-path references (cloud-sync engines and code don't mix; see spec 06).

## Frontmatter contracts (the load-bearing fields)

| File | Fields | Consumed by |
|---|---|---|
| `<Item>/00 - Brief.md` | `stage`, `client`, `deadline` (YYYY-MM-DD), `value`, `status` | pipeline card, raid boss, radar, phantoms, launch control |
| `Clients/<X>/_brief.md` | `last_touch` (YYYY-MM-DD), `poi_snooze` | aggro radar, POV reticles |
| agent writes | `source_agent`, `generated`, `confidence` (0–1), `target_path` | triage gate, marble run |
| dated notes | date in FILENAME | last-contact inference (never trust mtime under cloud sync) |

## The agent-write protocol

If the user runs writing agents (or wants the assistant to draft things):
- Agents write ONLY to `00_Inbox/from-agents/<agent-name>/` with the frontmatter contract above.
- A triage step (script or manual) promotes `confidence ≥ 0.85` writes to their `target_path`; holds the rest for review.
- Conflicts = versioned files (`<name>.agent-<ts>.md`). Last-write-wins is forbidden.
- Every agent gets `_agent_state/<agent>/` with `memory.json` (learnings ring) and `writes.jsonl` (audit: ts, target, confidence, action).

## Multi-model relay

`_relay/BATON.md`: current task state, done, blockers, next concrete action — written for a competent stranger; any agent (Claude/Codex/local) reads it first on multi-step work and updates it before stopping. `_relay/ISSUES.md`: the codified backlog — no issue lives only in chat; fixed issues graduate to `Preferences/Lessons.md`.

## The operating contract (CLAUDE.md / AGENTS.md)

Generate one for the user from their interview. Must encode at minimum: the lookup order (query `_brain_api/` JSON → then raw files), the agent-write protocol, the no-theater law, "one feature per commit", and the self-test habit. Keep it under 2 pages — contracts nobody reads protect nobody.

## Platform notes

Everything here is plain files — macOS/Windows/Linux identical. The refresh loop (spec 05) and voice pipeline (spec 02) are the only OS-specific parts.
