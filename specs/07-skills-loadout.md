# Spec 07 — The agent skill loadout (the other half of the system)

The vault is only half of this architecture. The other half lives on the **coding-agent side**: a stack of skills and plugins that change how the agent reads, writes, plans, and spends tokens. Without this loadout the agent treats the vault like a pile of markdown; with it, the agent behaves like a member of the system.

Everything below is public and installable. Keep each project's license + attribution intact.

## The stack

| Layer | Tool | What it changes | Install |
|---|---|---|---|
| Process discipline | **superpowers** (obra/superpowers) | Forces skill-first behavior: brainstorm before building, systematic debugging before fixes, verification before "done". The agent checks for an applicable skill before ANY action. | Claude Code plugin marketplace |
| Vault correctness | **obsidian-skills** (kepano/obsidian-skills, MIT) | Agent writes Obsidian-flavored markdown (wikilinks, callouts, properties), `.base` files, JSON Canvas — not generic markdown that breaks in Obsidian. Includes `defuddle` (clean web-page extraction) and the Obsidian CLI skill. | Plugin marketplace |
| Token discipline | **caveman** | Response compression (~60% fewer output tokens on prose) + `cavecrew` micro-agents (investigator/builder/reviewer) with caveman-compressed returns. The brain talks terse; substance survives, fluff dies. | Plugin marketplace |
| Command-level savings | **rtk** (Rust Token Killer) | CLI proxy that compresses dev-command output 60–90% (git/ls/test runners), wired as a Bash hook so it's transparent. Caveat from live ops: it can swallow stdout you actually need — expose an `RTK_DISABLE=1` escape hatch and use it when debugging. | cargo / release binary + Bash hook |
| Knowledge graph | **graphify** (`uv tool install graphifyy`) | Turns the vault (code, docs, anything) into a queryable knowledge graph. The lookup rule that makes it matter: **graph first, pre-computed JSON endpoints second, raw file reads last**. Enforce with hooks (see below). | `uv tool install graphifyy` |
| Cross-model plan hardening | **grill-me-codex** family (built on Matt Pocock's grill-me, MIT) | For high-stakes builds (auth, schema, concurrency, payments): one model interviews the user and locks the plan, a SECOND model (read-only) adversarially reviews it in rounds until APPROVED. Two models disagree more honestly than one model self-reviews. | skill files in `~/.claude/skills/` |
| Dev loop | **hot-reload** plugin + **Local REST API** plugin | `hot-reload` watches the plugin folder — saving `main.js` reloads it live (a `.hotreload` marker file arms it). The REST API lets the agent drive the running app: list/execute commands, verify a feature registered, prove a build is live. This pair is what makes "agent edits production plugin, verifies behavior, commits" a closed loop with no human clicking. | Obsidian community plugins |

## The domain skill library (pattern, not content)

Beyond the public stack, build the user their **own skill library**: one `SKILL.md` per repeatable task in their domain (proposal sections, meeting prep, status reports, code review checklists — whatever the interview surfaced). Rules that made the original library work at ~190 skills:

- **One skill = one job.** Description states exactly when to trigger. The agent scans the library before any non-trivial output.
- **Chained guards for anything external-facing**: humanize → quality bar → brand rules → **confidentiality guard, always last**. The guard scans for client names, code names, and regulated identifiers and returns PASS/FAIL with a redacted rewrite on FAIL. Nothing leaves the machine without it.
- **Self-learning loop, gated**: a registry JSON tracks per-skill run counts, user corrections, and health. ≥3 recorded corrections → the agent may *propose* folding them into the skill; the user approves every update; prior version archived for rollback; max one update per skill per week. Propose-only — never auto-edit skills.
- **Health watcher**: a nightly script validates each skill still resolves (paths, dependencies, schema) and escalates broken ones into the user's priority queue instead of letting them rot silently.

## Composition rules (what makes the stack a system)

1. **Lookup order is law**: knowledge graph → `_brain_api/*.json` endpoints → raw `Read`. Wire it as session hooks (SessionStart freshness brief, prompt-keyword prefetch, pre-Read endpoint enrichment) so it costs the agent nothing to comply.
2. **Skills before action.** The superpowers gate plus the domain library means the agent's first move on any task is "which skill applies", not "let me start typing".
3. **Token discipline compounds.** caveman (output) + rtk (command I/O) + graph-first lookup (context) routinely cut a working session's tokens by more than half. Cheaper sessions = longer sessions = fewer dropped batons.
4. **Cross-model review is a skill, not a vibe.** When the plan is irreversible (schema, migration), the loadout includes a second model on purpose. If the second model is unavailable (token-exhausted), substitute a panel of independent adversarial reviewers from the primary model and say so honestly in the build log.

## Build notes for the agent

- Install marketplace plugins first (superpowers, obsidian-skills, caveman), then CLI tools (rtk, graphify), then wire hooks. Verify each hook fires by checking the injected context in a fresh session before relying on it.
- Scale the domain library from the interview: start with 5–10 skills for the user's most repeated outputs. The library grows by correction, not by speculation.
- The confidentiality guard is **mandatory** before any feature that auto-sends or publishes. If the user's world has no confidential clients, build it anyway — it costs one skill file and the first leak costs more.
