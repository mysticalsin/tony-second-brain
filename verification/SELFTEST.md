# Verification — behavior, not load

## Per-phase gates (the building agent runs these)

**Phase 2 (plugin core):**
- `node --check main.js` (or build passes)
- health log shows `plugin loaded — build <current stamp>` after a hot-reload
- dashboard renders the user's REAL seed data (not placeholders); every empty card states what data to add
- edit a note → exactly ONE re-render fires (watch a render counter in the health log)

**Phase 3 (cost):**
- hand-compute one day's cost from one transcript with an independent 20-line parser (dedupe by message.id) → matches the card to the cent
- newest transcript's model id resolves to a price (not $0-unknown)
- Σby_day == Σby_model == all_time on every metric

**Phase 4 (voice):**
- text ask: a question whose answer exists ONLY in the vault returns the vault's fact
- a question whose answer is NOT in context → the assistant says it doesn't know (grounding holds)
- if voice: wake word opens mic (state Notice shown); spoken reply plays end-to-end; barge-in cancels
- kill the TTS daemon mid-session → reply still arrives via fallback chain, engine sticky per reply

**Phase 5 (effects):**
- run a real brain turn → sparks land on the ACTUAL files read (verify against the CLI's tool log)
- thinking state → neuron cascades visibly run (this exact feature shipped dead once — watch it)
- spam 30 tool events → live spark count never exceeds the cap; overlay disappears ~4s after idle
- reload plugin mid-effect → no orphaned SVG/timers (DOM inspect)

**Phase 6 (features):** one behavioral demo per feature, driven end-to-end (click the ghost → file materializes; mark-touched → blip knocked back; etc.)

## §command — the in-plugin self-test (build this, always)

A command "Self-test: prove the build is alive" that checks BEHAVIOR and prints a pass/fail table to a modal + health log:

1. build stamp in health log matches `PLUGIN_BUILD` constant (live code = current code)
2. data layer: each registered fetcher returns non-error within its TTL
3. ThreatIndex: map non-empty OR honestly reports "no entities in scope"
4. phantom manifests: parse + zero ghosts for artifacts that exist (sample check)
5. synapse: `setState('thinking')` toggles the DOM class (the dead-feature detector)
6. voice (if enabled): recall daemon `/health` OK or "cold" stated; TTS chain reports which engines are up
7. cost: newest transcript model resolves to a price; stats invariant holds
8. no `unhandledrejection` entries in the health log since last build stamp
9. flag-file vs settings visibility state agree
10. every registered interval has fired since load (heartbeat counters)

Run it: after every build bump, in CI if they have it, and weekly by habit.
