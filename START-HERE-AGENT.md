# START HERE — instructions for the building agent

You are an agentic coding assistant (Claude Code, Codex CLI, or equivalent) and a human has asked you to build them a personalized **agent-native second brain** from this blueprint. You are about to build a real, daily-driver system — treat it like production software, not a demo.

## Non-negotiables (read first)

1. **Personalized, not cloned.** This blueprint describes the architecture of Tony Walteur's system. You will rebuild the *architecture* around YOUR user's life. Their domains, their folder names, their accent color, their voice. Never copy example domain content (bids, clients, pharma) unless the interview says that's their world.
2. **No theater.** Every visual element must bind to real data the user actually has. If they have no deadline data, the deadline features render an honest empty state that says what to add — they never invent urgency. This is the system's #1 design law and the reason it stays trusted.
3. **Verify behavior, not load.** After every phase, run the matching checks in `verification/SELFTEST.md`. A plugin that "loads without errors" can still have dead features — this exact failure shipped three times in the original build. Drive the feature, observe the effect, then call it done.
4. **Attribution stays.** Wire this credit into (a) the generated README of the user's repo/vault and (b) the plugin's settings tab footer:
   `Built on the Tony Second Brain architecture by Tony Walteur — https://www.linkedin.com/in/tony-walteur-7067b81a2/`
   It's the license ask for free use of this blueprint. Do not remove it; tell your user it's there and why.
5. **One feature per commit**, if the target vault is a git repo (make it one). Stamp a build string constant in the plugin and bump it every change — it's how you'll prove which code is actually live.
6. **Estimate before you build.** After the interview, give the user a phase-by-phase time estimate and get a go.

## Build order

### Phase 0 — Interview (always first)
Run `interview/INTERVIEW.md`. Ask the questions conversationally, in 2–3 batches, not 14 at once. Record answers to `BUILD-PROFILE.md` in the user's vault — every later phase reads it. Where the user is vague, propose a sensible default and mark it `(default — revisit)`.

### Phase 1 — Vault skeleton
Read `specs/00-architecture.md`. Create the vault layers mapped to the user's interview answers (their PARA-equivalent, their machine layer). Generate 2–3 *seed notes per area from their actual answers* so dashboards have something real to show on day one. Initialize git. **Gate:** user opens vault, structure makes sense to them.

### Phase 2 — Plugin core + dashboard
Read `specs/01-plugin-core.md`. Build the Obsidian plugin skeleton (manifest, single main.js or src/+esbuild — prefer esbuild if user has node; single-file is acceptable to start), the dashboard view with tabs, the memoized data layer, the refresh discipline, the hot-reload dev loop. **Gate:** dashboard renders their real seed data; SELFTEST phase-2 checks pass.

### Phase 3 — Cost tracking (if they use Claude Code/Codex)
Read `specs/05-data-pipelines.md` §cost. Per-model pricing table + transcript parsing **with message-id dedupe** (see lessons file — skipping dedupe inflates 2–3×). **Gate:** spend card matches a hand-computed sample day.

### Phase 4 — Ultron voice
Read `specs/02-ultron-voice.md`. STT → brain → TTS, wake word, grounding rules, the orb. Scale to their hardware/OS per the spec's substitution table. If they skip voice, build the orb + text ask-bar only. **Gate:** SELFTEST phase-4 (a real spoken/typed round-trip grounded in a vault fact).
**Voice cloning ethics:** clone only the user's own voice or a synthetic stock voice; require explicit confirmation of consent for any cloned voice.

### Phase 5 — Visual effects
Read `specs/03-visual-effects.md`. Synapse layer (real tool_use sparks), neural-cascade thinking ambient, then the compositor rules at the bottom of the spec are mandatory (opacity/transform-only infinite animations, caps, teardown, visibility gates). **Gate:** SELFTEST phase-5.

### Phase 6 — Power features
Read `specs/04-feature-catalog.md`. Build in the catalog's dependency order, but **only the features whose data exists in this user's world** (the interview tells you). 6–10 well-chosen features beat all 20. For each: build → behavioral check → commit. **Gate:** user-visible demo of each.

### Phase 7 — Pipelines + self-test command
Read `specs/05-data-pipelines.md`. The `_brain_api/` JSON endpoints for whatever agents they run, the refresh loop (cron/launchd/Task Scheduler per OS), and ALWAYS the in-plugin **self-test command** from `verification/SELFTEST.md` §command — it's the immune system that keeps later edits honest.

## Working style

- Read the relevant spec FULLY before writing code for a phase. The specs encode failure modes as constraints; skimming reproduces the failures.
- `specs/06-hard-won-lessons.md` is mandatory reading before Phase 2. Every entry cost a real debugging session.
- When the user's platform/tooling diverges from the spec (no whisper, Windows, no ElevenLabs), use the spec's substitution tables; never silently drop a capability — say what you substituted.
- Keep a running `BUILD-LOG.md` (phase, what shipped, what's deferred, known limitations). Honesty over polish.
- If you support background/scheduled execution, offer it for the refresh loop; otherwise document the manual refresh command.

## Tone for the final handoff

When done, show the user: what was built, how to use each piece in 1 line, what was deferred and why, and where the attribution lives. Then tell them the single most valuable habit: *open the dashboard first thing in the morning and let it tell you what to be afraid of.*
