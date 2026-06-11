# Spec 08 — Relay: one worker, many models (the cross-model baton)

Claude, Codex, Gemini, and local models (Ollama / LM Studio) are not separate tools in this architecture — they are **consecutive shifts of one continuous worker**. The vault is the shared memory; the baton is how a shift changes hands without dropping the thread.

The mental model is a **nurse's shift handoff**: the nurse going off-duty tells the one coming on exactly what's true right now, what's done, what's pending, and the plan forward — so care never restarts from zero.

> **The whole point:** the next model picks up *exactly* where the last one left off. No re-discovery, no re-deciding settled questions, no lost context. This is what makes session limits, context windows, and model outages survivable: when one shift dies mid-task, the next reads the baton and continues — any model, any vendor.

## The folder

Create `_relay/` at the vault root:

| File | What it is | Who writes it |
|---|---|---|
| `BATON.md` | The **live** shift-state — single source of truth for the work in flight. Always current. | The model currently on shift, continuously |
| `log.md` | Append-only one-line ledger of every handoff (date · model · headline). The history. | Each model, on handoff |
| `ISSUES.md` | The standing **attack queue** — everything known-but-unfixed, prioritized (id · severity · symptom · next action). Found issues land here; fixed ones graduate to the user's lessons file and get struck. | Any shift, when it finds or fixes something |
| `archive/` | Closed batons (`YYYY-MM-DD-<slug>.md`), moved here when a task finishes. | The model that closes the task |
| `README.md` | This protocol, in the vault, so ANY model pointed at `_relay/` has the full spec in plain markdown. | Written once at build time |

`BATON.md` = the task in flight. `ISSUES.md` = the backlog of known wounds. Read **both** at the start of every build session. Normally there is ONE active baton; if two unrelated tracks truly run at once, namespace (`BATON.<track>.md`) — and keep that to a minimum, because the baton works precisely because there's one obvious place to look.

## The protocol (every model, every shift)

1. **On shift start — READ FIRST.** Before doing anything on a multi-step task, read `BATON.md`. Do not re-investigate what it already settled. Empty or clearly-stale baton → you're starting a fresh one; that's fine.
2. **During the shift — KEEP IT TRUE.** The baton reflects reality *now*, not the plan from an hour ago — especially **Done**, **Blockers**, **Next steps**. A stale baton is worse than none: it lies to the next shift.
3. **On handoff — HAND OFF CLEANLY.** Before stopping (context running out, switching models, blocked, done for now): make `BATON.md` self-contained — the next model has **none of your conversation, only this file**. Stamp the footer (`Last updated` / `By` / `Next shift`). Append one line to `log.md`. If the task is finished: move the baton to `archive/` and reset from the template.
4. **Golden rule: write for a competent stranger.** Absolute paths, real file names, concrete next action. No "as discussed", no pronouns without antecedents. If the next shift can't continue from the baton alone, the handoff failed.

## Wiring each model to the protocol

Each model auto-loads a different instruction file — point all of them at the same protocol:

| Model | Entry file | Note |
|---|---|---|
| Claude Code | `CLAUDE.md` § Relay | points at `_relay/README.md` |
| OpenAI Codex | `AGENTS.md` § Relay | same section, **adapted** — see pitfall below |
| Gemini CLI | `GEMINI.md` § Relay | same |
| Local model (Ollama / LM Studio / custom) | `HERMES.md` (or `<MODEL>.md`) | a local model auto-loads nothing — whatever drives it must **inject the entry file + the current `BATON.md` as system context** at shift start. Once fed, it follows the protocol like the rest. |

**Pitfall (cost a real debugging session):** do NOT generate the per-model entry files by find/replacing the model name over `CLAUDE.md`. A naive `Claude→Codex` sed produced instructions referencing settings files and model IDs that don't exist, silently misleading every Codex session that read them. Each entry file is an *adaptation* (right model IDs, right config paths, right capabilities), not a string substitution.

## Why this earns its keep (real incidents, original build)

- A 50-agent pull job hit the provider's **session limit mid-run** at 22/50. The next shift read the baton, diffed disk against the input set, and backfilled exactly the 28 missing items — zero re-pulls, zero re-decisions.
- Multi-day feature builds survived **model switches** (planning on one model, execution on another, review on a third) because phase state, locked decisions, and gotchas lived in the baton, not in any one session's context.
- The **attack queue** stopped "found a bug while doing something else" from evaporating: log it in `ISSUES.md`, finish your shift, let a later shift attack it deliberately.

## `BATON.md` template (copy when starting a fresh task)

```markdown
# 🪃 BATON — <task name>

**Task:** <one sentence: what we are building / fixing / deciding>
**Status:** active | blocked | done
**Started:** <date> by <model>

## State now
<what is true at this moment — the situation the next shift inherits>

## Done
- <completed step, with the file/command that proves it>

## In progress / blockers
- <what's mid-flight, or what's blocking and why>

## Next steps (the plan forward)
1. <concrete next action — file, command, decision>
2. <...>

## Context / files touched
- `<vault-relative path>` — <why it matters>
- Links: [[note]] · related batons · tickets

---
_Last updated: <date> · By: <model> · Next shift: <first thing the next hand should do>_
```

## Build notes for the agent

- Create `_relay/` with README (this spec), empty `log.md`, `ISSUES.md` with the entry-format header, `archive/.gitkeep`, and `BATON.md` reset to the template.
- Wire the § Relay section into every entry file the user's models actually load — and only those.
- The relay folder is **versioned** (commit it): the baton history is part of the system's memory and survives disaster recovery.
- Personal/client content does not belong in batons pushed to any public remote. The baton lives in the user's private vault repo; this spec is what's public.
