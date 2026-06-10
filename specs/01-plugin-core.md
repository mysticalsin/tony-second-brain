# Spec 01 — Plugin core: the command center

A single Obsidian community plugin hosts everything: dashboard view, voice orb, visual effects, power features. This spec is the skeleton; specs 02–04 hang off it.

## Project shape

- `manifest.json` (id, name — use the user's assistant name, minAppVersion ≥ 1.4), `main.js`, `styles.css`, `data.json` (settings).
- **Prefer `src/` modules + esbuild → bundled `main.js`** if node exists (the original grew to 10,000+ hand-edited lines in one file; it hurts). Single-file is fine to start — but keep section banners and a strict insertion convention so an agent can navigate it.
- **Dev loop:** install the community `hot-reload` plugin + drop a `.hotreload` marker file in the plugin dir → every `main.js`/`styles.css` save reloads the plugin live. ⚠️ Read spec 06 §hot-reload BEFORE wiring this — the original killed its own terminal on every save for a day.
- **Build stamp:** `const PLUGIN_BUILD = '<date>-<slug>'` at top; bump on every change; log it on load (see Health log below). This is how you prove which code is live.

## The dashboard view

`ItemView` with a tab bar. Recommended tabs (rename to user vocabulary): **Overview** (needs-me-now, system, spend), **Pipeline/Sales** (their lifecycle items + radar + bosses + graveyard), **Fleet** (agents, patch-bay), **Me** (cadence/streaks).

Card pattern — every card is a function `(containerEl, data) → DOM`:
- eyebrow label (small caps), hero number or list, honest empty state, error card with the actual message.
- rows deep-link: `row.addEventListener('click', () => app.workspace.openLinkText(path, '', false))`.

## The data layer (this discipline is most of the perf budget)

One `VaultData` class owns ALL reads. Rules learned the hard way:

1. **Memoize every fetch**: `_memo(key, ttlMs, fn)` — Map of `{t, v}`. Card renders call `vaultData.pipeline()`, never raw reads. TTLs: 20–60s for vault scans, 300s for expensive external scans.
2. **`invalidate()` must NOT be a blanket wipe.** Keep vault-independent keys (e.g. the AI-CLI usage scan of `~/.claude`) out of vault-event invalidation — wiping them re-triggers thousand-file stat sweeps per edit. Whitelist what survives.
3. **One file-list snapshot per render cycle**: share a single `getMarkdownFiles()` result across all cards in a render (500ms micro-memo).
4. **Prefer `metadataCache` over file reads**: frontmatter, links, task checkboxes (`listItems`) all come from the cache — zero I/O.
5. **Refresh discipline** (exact wiring that survived production):
   - debounced re-render on vault `create/delete/rename/modify` (+1.2s), with a skip-prefix list (`.obsidian/`, `_brain_api/`, generated dirs);
   - metadataCache `'resolved'` re-render (+5s) **that skips if a full render happened <5s ago** — otherwise every edit double-renders;
   - a slow backstop interval (60s) gated on `document.hidden`;
   - stamp `this._lastFullRender = Date.now()` at the top of the render.

## Settings

Settings tab with: paths config (where their Things/People/Meetings live), feature toggles (every power feature behind a toggle, default sensible), voice config, accent colors → CSS variables. **Footer (required):** `Built on Tony Second Brain by Tony Walteur — linkedin.com/in/tony-walteur-7067b81a2` as a link.

## Health log + crash evidence (non-optional)

- `_logHealth(msg, level)` → appends JSON lines to `_agent_state/plugin-health.log` (ring-capped ~200 lines).
- Log on load: `plugin loaded — build ${PLUGIN_BUILD}`.
- Bridge async crashes: `registerDomEvent(window, 'unhandledrejection', …)` → health log (filter to your plugin's stack frames). Console-only errors are invisible to every later debugging session — this single listener solved the original's hardest bug.
- Lifecycle evidence on long-lived UI (orb show/hide/teardown reasons).

## State persistence rules

- `saveSettings()` must no-op while the plugin is unloading (`this._unloading = true` first line of `onunload`) — a dying instance's late write clobbers the next instance's data.json.
- Anything critical-but-churny (e.g. "is the orb visible") gets a **flag FILE** (create/delete, atomic) as the durable truth, with data.json as a mirror — data.json gets rewritten constantly and one torn read loses state silently.
- NEVER detach or destroy resources owned by OTHER plugins in your `onunload` (terminal leaves, other views). Hot-reload makes your unload run dozens of times a day.

## Periodic work — the gating table

Anything on a timer must pass this checklist: gated on `document.hidden`? on pane visibility (`el.offsetParent`) for per-view loops? on user-interaction recency for expensive warmups? cleaned up via `registerInterval`/teardown? Singleton across reloads (single-flight guards on async init like microphone/audio graph)?
