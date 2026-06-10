# Spec 06 — Hard-won lessons (every entry cost a real debugging session)

Read before Phase 2. These are constraints, not suggestions.

## The meta-lesson
**"Hot-reload verified: loaded clean" verifies the LOAD, not the feature.** Three features in the original shipped dead-on-arrival and survived multiple reviews: an ambient animation gated on a CSS class nothing ever set; a 280-line "cardiac system" class that was never instantiated; a method call to a function that had been moved in a refactor (it killed a core feature silently for days). Antidotes: the behavioral self-test command (verification/SELFTEST.md), grep-for-reads before claiming a flag/guard works, and driving every feature once before calling it done.

## Plugin lifecycle
- **Never touch other plugins' resources in `onunload`.** The original detached all terminal leaves "to prevent PTY leaks" — hot-reload runs unload on every save, so every code edit murdered the user's live terminal session. Other plugins' views survive your reload fine; leave them alone.
- **A dying instance's async saves clobber the next instance.** `this._unloading = true` first line of onunload; `saveSettings()` no-ops when set.
- **Critical state needs a flag file, not data.json.** data.json is rewritten constantly (voice turns, clicks); one torn/stale read silently flips state (the orb's visibility died this way — "appears, then disappears after the next reload"). Atomic create/delete flag file = durable truth; reconcile + log on load when they disagree.
- **Bridge `unhandledrejection` to a persistent log on day one.** An async `show()`-style flow that throws mid-body leaves visible UI but skips everything after the throw (including persistence) — and the error exists only in a devtools console nobody has open. One window listener turned the original's hardest bug into a one-shot diagnosis.
- **Layout-ready is a storm.** Everything wants to start there (UI assembly, watchdogs, indexes, daemons). Keep the tick for user-facing UI; stagger the rest +2.5s/+4s/+8s.
- **Watchdog guard flags must be READ somewhere.** The original's "don't resurrect a terminal the user closed" flag was written on every close and read by nothing. grep for reads.
- **Single-flight every async initializer** (mic/audio graph, terminal ensure, daemon spawn). Two racing callers = duplicate resources; with getUserMedia it's a leaked live microphone.

## Sync + scheduled jobs (cloud-synced vaults)
- **mtimes lie.** Sync engines rewrite them; "last contact" inference must use dates in FILENAMES or frontmatter.
- **macOS TCC:** launchd-scheduled non-Apple binaries (uv/python) can see a cloud-synced folder as EMPTY (per-binary consent). Stage with Apple binaries (rsync) to a local dir, process there.
- **Keep pipeline code outside the synced folder.** Sync engines evict/churn; code doesn't belong in transport.

## Cost tracking
- **Transcript lines duplicate usage per content block — dedupe by message.id or inflate 2–3×.** Cross-validate with an independent parser once.
- **A new model family prices at $0 silently** if your table only knows the old families. Family-fallback + log-unknowns + a self-test that prices the newest transcript's model.
- **One source of truth for spend** (stats.json); never compute the same number two ways in two surfaces — they WILL disagree and the user will trust neither.

## Voice
- Persona must not promise actions the toolset can't do — the model fills capability gaps with confident fiction.
- Filter poisoned history on load (transcription garbage replays its style into every future reply).
- Positional reply queues: never splice timed-out waiters (wrong-sentence audio); mark-dead-keep-slot.
- Don't gate engine spawn on `navigator.onLine` (lies in Electron); probe with a short curl timeout instead.
- Proactive speech: stamp delivered-flags AFTER the gated speak succeeds, with a separate written-flag — or daily slots burn silently.
- A daemon probe that spawns `process.execPath` thinking it's node will fork-bomb crash children inside Electron (runAsNode fuse off) — probe with `/usr/bin/curl`.

## Rendering
- Infinite animations: opacity/transform only. Box-shadow/filter keyframes = full repaint per frame forever.
- Dynamically-inserted SMIL needs `begin="indefinite"` + `beginElement()` or it renders frozen (and fades mask it).
- Canvas loops: fps-cap, `offsetParent` idle for background panes, die with the canvas.
- MutationObservers that inject DOM must ignore their own mutations.
- Per-paint file reads compound invisibly — memoize everything through one data layer, and never blanket-invalidate caches whose source the event can't have changed.

## Process
- One feature per commit + build-stamp bump + health-log "loaded build X" line = always-shippable, always-provable state.
- An ISSUES.md attack queue: found-but-unfixed goes there, never only in chat; fixed graduates to a lessons file (this one).
- Git checkpoint before editing any 1000+-line hand-maintained file.
