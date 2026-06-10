# Spec 04 — The power-feature catalog

20 features, each daily-useful AND visually alive. Build ONLY the ones whose data exists in the user's world (interview-mapped). Order below = dependency order. Vocabulary: `<Item>` = their lifecycle thing (deal/paper/case), `<Entity>` = their relationship (client/investor/editor).

Shared substrate (build first):
- **ThreatIndex** (plugin service): map of entity-path → `{level: healthy|monitor|threat, reasons[]}` from: last-contact (newest DATED Meetings/Daily note linking to the entity via `metadataCache.resolvedLinks` — filename dates, never mtime), frontmatter `last_touch`, item deadlines from the brain-api, `poi_snooze` override (human wins). Rebuilt on metadata `'resolved'` (30s debounce) + 5-min hidden-gated timer. O(1) lookups at render. **Receipts mandatory: no evidence → stays healthy.**
- **Phantom manifest** (script, spec 05 refresh loop): per open item, diff its folder against a stage-conditioned "winning skeleton" (`_meta/winning-skeleton.yaml`: stage → expected artifacts + match globs + provenance `doctrine|learned`) → `_brain_api/<item>/phantoms.json`. Accent-insensitive generous glob matching; ZERO false ghosts is the acceptance bar.

---

### 1. Phantom Files ★ (explorer)
Ghost rows inside item folders for stage-due artifacts that don't exist. Translucent `.nav-file` rows injected under the folder (MutationObserver re-injects after explorer re-renders — and must IGNORE mutations caused by its own nodes). Breathing opacity pulse, faster as deadline nears (`urgency_days`). Hover = evidence + provenance ("doctrine" until ≥5 closed items teach it). Click = materialize: copy template or provenance-stamped stub, open it, fire a synapse spark. Real file matching the globs → ghost dissolves (vault `create` watcher).

### 2. Machine POV ★ (reading view)
Targeting reticles on entity wikilinks: 4-corner AF brackets via 8 background-gradients (no extra DOM). White healthy / amber monitor (silence ≥21d or deadline ≤14d) / red threat (≥35d or ≤7d). Hover = the receipts. Markdown post-processor resolving links via `getFirstLinkpathDest` → ThreatIndex lookup. (CM6 live-preview decorations = v2.)

### 3. Aggro Radar ★ (dashboard)
Canvas radar; every entity with a contact signal is a blip; distance from center = silence-days (45d = center), bearing = stable name-hash; threat blips pulse. Sweep beam surfaces "name + days" as it passes. Hover = receipts; click = open brief. Action row for the top aggressor: **Draft check-in** (creates a review-before-send draft in Outbox/ quoting the trigger) + **Mark touched** (`processFrontMatter` writes `last_touch`, rebuild, blip knocked back). 30fps cap + offsetParent idle (spec 03).

### 4. Raid Boss Deadlines (dashboard)
Each open item = a boss; HP = unresolved gate items (phantoms + unchecked `- [ ]` tasks in its folder via `metadataCache.listItems` — zero reads). HP bar with damage float (`-N ⚔`) when count drops between paints. ≤72h to deadline = telegraph: pulsing red box listing the exact blockers, clickable.

### 5. Corpse Run (dashboard)
Graveyard strip of closed items lacking a retro/debrief file. Loot glow decays over 14 days from close date, then "crumbled" (still runnable, honestly labeled). Click = create + open a schema-valid debrief stub (outcome, what-decided-it, do-differently) the learning pipeline consumes.

### 6. Marble Run (dashboard, needs agent writes)
Recent triage events from the triage log roll in as glass marbles: green promoted, amber held (drops toward tray), red rejected, grey duplicate-suppressed. Hover = the log line. Held-count tray rattles (transform animation), click = open review flow. 60s memo on the log read.

### 7. Launch Control (command)
Pre-submit go/no-go board for an item: stations run REAL checks sequentially with lamps — frontmatter completeness, deadline math, phantoms==0, content lint (their domain's rules), name-screening against an off-limits list, quality floor. Red rows deep-link to evidence. All-green unlocks "write launch record" (auditable note). ABORT stops. Nothing auto-submits, ever.

### 8. Diagnostic Chamber (command, needs agents)
Fuzzy-pick an agent → dark analysis-room modal: its memory/stats/last-writes fanned out as attribute cards + a deposition box. Questions go to the agent CLI framed as the agent itself: *first person, strictly from the state files below, name what's missing rather than invent*. Optionally spoken via TTS.

### 9. Time-Scrub Cinema (command)
Active note becomes a film of its git history: slider over last ~40 commits (`git log --follow`) + working tree as final frame; text morphs between versions; lines new in the scrubbed-to version flash heat (set-diff vs previous frame). Header: date · author · subject. 800-line render cap.

### 10. Vault CCTV (command)
24h dial over a floor-plan grid of top-level folders. Three real event tapes merged: git commits (file-level, author), agent-triage log entries, raw mtime churn unexplained by commits. Scrub lights folder boxes whose files changed in the trailing 25-min window (color per source) + live event feed; ▶ replays 24h in ~12s.

### 11. The Forge (command)
Craft a reusable content block from 3 ingredients: pain-point (text), differentiator (text), proof picked from their canonical-blocks library (only cite the chosen block; no proof = output carries `[PROOF NEEDED]` visibly). Anvil button, spark animation, result lands in Inbox via the agent-write protocol (held for review — external-facing).

### 12. Loadout Screen (command)
Pre-meeting draft pick: inventory cards (entity brief = rare border; canonical blocks = legendary ONLY if they carry real performance data, else "unproven"; recent meeting notes). Equip ≤4 → writes a loadout note to Meetings/prep/ with blocks inlined — which the recall index then serves to the voice brain in the room.

### 13. Sparring Chamber (command)
Rehearse against a counterpart built strictly from their corpus (entity brief + recent notes): it objects in character, forbidden from inventing entity-specific facts; optionally speaks. END BOUT = 4-line telemetry from the transcript: LANDED / THIN / UNANSWERED / NEXT REP.

### 14. Document Tomography (command)
MRI mode over a structured extraction of an inbound document (their extractor populates `<item>/doc-model.json`: obligations, criteria, dates, requirements + a risk file). Scroll-wheel slices translucent strata; inactive layers blur/dim by depth; flagged inferences labeled honestly; deep-links to evidence files.

### 15. Feed the Orb (orb)
Drag any file onto the orb → chew animation → extract text (markitdown/pandoc for binary formats, 2MB cap, ≤3 files) → brain digests (what-it-is / key points verbatim / action line) → lays a frontmattered note into Inbox via the agent-write protocol. Refusals named honestly.

### 16. Secret Doors (explorer, needs recall daemon)
Per open item, ask the recall daemon what else resonates (similarity ≥0.45, outside own folder, junk filtered, top 3) → tiny glowing door rows under the folder. Open = mint a bridge note carrying the evidence (score, source) linking both sides. Daemon cold = no doors, silently. Glow animates OPACITY only.

### 17. Invisible Ink (command)
On the active note: fog overlay while the brain compares it against the brain-api brief + recall context → gaps/contradictions fade in as ink lines, each forced to cite its source or be dropped; "NOTHING" is an allowed honest answer. Click dissolves.

### 18. Patch-Bay Graph (dashboard, needs agents)
The fleet as a modular synth: agent jacks left, target-folder jacks right (from each agent's writes.jsonl), cable thickness = volume (log), brightness = recency, stale agents hang slack grey, never-wrote = unplugged. Hover = counts. 5-min memo on the scan.

### 19. Deck/Doc X-Ray (command)
Brand-lint their outbound format (e.g. pptx = zip → slide XML via `unzip -p`): off-palette colors, banned-phrase list, off-brand fonts; per-unit findings; "fix at the source, never hand-edit the artifact" reminder.

### 20. Self-Test (command — MANDATORY, not optional)
See `verification/SELFTEST.md` §command. The immune system. Build it in Phase 7 no matter which other features shipped.
