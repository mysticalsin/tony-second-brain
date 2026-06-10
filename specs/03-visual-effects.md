# Spec 03 — Visual effects: watching the AI think

The wow layer. All of it obeys law #1: every effect is driven by real events.

## The synapse layer (the signature effect)

While the brain processes a turn, every file it ACTUALLY touches sparks in the file explorer.

**Data source — real, not simulated:** run the agent CLI with JSON streaming (`claude -p --output-format stream-json` or equivalent) and parse assistant `tool_use` blocks from stdout. Each block with a `file_path`/`path` input inside the vault → fire a spark at that file's explorer row.

**Rendering (one persistent SVG overlay on `document.body`, `pointer-events:none`, z-index below the orb):**
- Axon: quadratic bézier from previous target (or orb center) to the target row's rect — perpendicular bow ±(min(120, dist·0.25)) so paths curve like dendrites. Draw-in via WAAPI `strokeDashoffset` animation (length → 0 over ~0.55–0.75s), then fade; self-remove.
- Spark head: small circle riding the path via SMIL `animateMotion`. ⚠️ MUST set `begin="indefinite"` and call `beginElement()` after insert — a dynamically-inserted SMIL animation with default begin resolves against the overlay's clock and renders FROZEN at the path end (the fade masks it; you'll ship it broken and not know).
- Row flash: arrival-synced class toggle on the `nav-file-title` (accent flash keyframe ~1s, remove-void-offsetWidth-add to restart on rapid re-hits).
- Folder targets (Grep/Glob over a directory): match the folder's OWN row first, THEN walk up to the deepest visible ancestor; top-level folders are matched by the first query (off-by-one here sends sparks to the wrong rows).

**Caps & teardown (mandatory):** hard cap ~14 live sparks **enforced in the actual fire path** (not just the ambient path); `document.hidden` no-op; overlay self-removes 4s after idle; `_dead` flag set in destroy() checked by every timer callback (zombie timers re-arm otherwise).

## Neural cascade (ambient "thinking" mode)

While state == thinking (between tool calls), thought propagates as **neuron chains**: every 0.9–2s, an action potential leaves the orb, hops row→row across visible explorer rows (2–4 hops, prefer targets <320px from the previous — chains read as anatomy, not lightning), with a soma membrane-pulse (small circle, WAAPI scale 1→4 + fade, `transform-box: fill-box; transform-origin: center`) at each arrival, and ~30% chance to fork a branch. Respect the same caps.

**Wiring trap that shipped dead once:** the thinking state must REACH the DOM. If your orb engine's `setState` is pure WebGL, wrap it: `const raw = orb.setState; orb.setState = s => { wrap.classList.toggle('thinking', s==='thinking'); raw(s); }` and drive the cascade from a MutationObserver on that class. Verify by behavior (cascade visibly runs), not by code review.

## The orb

WebGL particle sphere (three.js bundled separately, loaded once via `new Function(bundleCode)` from a prebuilt IIFE exposing `createOrb(canvas) → {setState, setAnalyser, destroy}`). States = color/turbulence presets; `setAnalyser` feeds mic FFT for audio-reactive ripples while listening/speaking. Handle `webglcontextlost`: heal once (teardown + re-show, guarded single-flight). Keep the bundle minimal; cap device pixel ratio at 2.

If WebGL is too heavy for the user's machine: a CSS-only orb (layered radial gradients + subtle scale breathing) preserves 80% of the charm — make it the interview's low-spec option.

## Dashboard motion

- Radar sweep: canvas, conic-gradient beam, ~30fps cap (`now - lastFrame < 33 → skip`), **1Hz idle when `!canvas.offsetParent`** (background workspace tab keeps canvases connected-but-hidden — they'll paint unseen frames all day otherwise), loop dies with the canvas (`!isConnected → return`).
- HP-bar hits, marble roll-ins, ghost condensation, tomography slicing: one-shot CSS animations on state change — fine.

## Compositor rules (mandatory for ALL effects)

1. **Infinite animations may animate `opacity`/`transform` ONLY** (compositor-only). Animating `box-shadow`/`filter: drop-shadow`/`background` infinitely = full repaint every frame, forever. One-shot/transient shadows are fine.
2. Forced layout (`getTotalLength`, `offsetWidth`, `getBoundingClientRect` after a write) is acceptable ONLY under a hard cap on instances per frame.
3. Everything periodic: `document.hidden` gate + per-pane `offsetParent` gate + teardown with the owning instance.
4. Stagger boot: heavy initializers (index builds, explorer injections, daemon spawns) go +2.5s/+4s/+8s after `onLayoutReady` — never in the layout-ready tick itself (the orb and dashboard own that tick; boot must feel instant).
