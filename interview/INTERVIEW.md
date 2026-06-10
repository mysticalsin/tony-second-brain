# The personalization interview

Run conversationally in 2–3 batches. Write results to `BUILD-PROFILE.md` in the user's vault root. Propose defaults when answers are vague; mark them `(default — revisit)`.

## Batch 1 — Their world (drives vault structure + features)

1. **What's your work in one sentence?** (role, industry — drives vocabulary everywhere: "bids" vs "deals" vs "papers" vs "cases")
2. **What are the 3–5 THINGS you track that have lifecycles?** (e.g. deals: lead→won/lost; papers: draft→submitted→published; patients, projects, releases…) → these become the pipeline, the radar blips, the raid bosses, the phantom skeletons.
3. **Who are the PEOPLE/ENTITIES that go cold if you neglect them?** (clients, investors, collaborators, editors) → Aggro Radar + Machine POV reticles.
4. **What does a finished unit of work look like, and what artifacts does a GOOD one always have?** (e.g. a good proposal has: pricing memo, red-team review, exec summary) → the phantom-files "winning skeleton".
5. **What recurring documents land on you that you'd love auto-digested?** (RFPs, contracts, papers, reports) → Tomography layers + Feed-the-Orb.

## Batch 2 — Their stack (drives implementation choices)

6. **OS?** (macOS = full voice experience; Windows/Linux = substitution table in specs/02)
7. **Which agentic CLI(s) do you have?** Claude Code / Codex / other — and subscription or API key? (the brain; everything works keyless via CLI subscriptions)
8. **Existing Obsidian vault or fresh?** If existing: NEVER restructure their notes — build the machine layer and plugin AROUND them; map their existing folders to the architecture.
9. **Node.js available?** (yes → esbuild module build; no → single-file plugin)
10. **Always-on machine or laptop?** (drives refresh-loop scheduling + battery gates)

## Batch 3 — Their taste (drives the skin)

11. **Voice: want it?** If yes: their own cloned voice (consent confirmed), a stock voice, or system TTS? Wake word preference? (default: "Ultron" is taken — suggest they pick their own assistant name; it personalizes everything)
12. **Assistant persona in one line?** (calm and surgical? warm? terse?) → brain system-prompt style block.
13. **Accent colors** (two hex values; default purple `#6600AE` + yellow `#F8F060`) and dark/light.
14. **Daily rhythm:** when does the day start (morning digest time), quiet hours (no spoken alerts)?

## Derivations the agent must make (don't ask, infer)

- **Feature shortlist:** map answers 2–5 to the catalog: lifecycle things → Pipeline/Radar/Boss/Corpse-Run/Phantoms; people → POV/Loadout/Sparring; documents → Tomography/Feed-the-Orb/Forge. Cut features whose data-source answer was empty.
- **Vocabulary map:** {their word for deal, their stages, their entity types} → used in ALL UI strings, folder names, and the brain's system prompt.
- **Empty-state plan:** for each shipped feature, the honest empty state text that tells them what data to add.
