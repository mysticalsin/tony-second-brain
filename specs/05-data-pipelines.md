# Spec 05 — Data pipelines: the machine layer

## `_brain_api/` — query endpoints, not folder crawls

A build script (`build_brain_api.py`, python3 stdlib) regenerates JSON views from vault frontmatter:

```
_brain_api/
├── _manifest.json                 # what exists + generated-at
├── items/_open.json               # every open <Item>: {id, path, entity, stage, deadline, value}
├── items/<id>/status.json         # per-item detail
├── items/<id>/phantoms.json       # missing-artifact manifest (spec 04 substrate)
├── entities/<name>/brief.json     # distilled relationship state
└── changes/since_<ts>.json        # recent-changes feed for agents
```

Rules: generated = gitignored = disposable; committed truth lives in the Markdown frontmatter and `_agent_state/`. Scripts live OUTSIDE cloud-synced folders if the vault is cloud-synced (sync engines + scheduled python = TCC/permission pain on macOS; keep code in `~/brain-build/`, vault reached by absolute path; if scheduled jobs can't read the cloud path, rsync-stage with system binaries first).

## The refresh loop

One `refresh.sh` orchestrates: build_brain_api → phantom manifests → recall re-index staging → health checks. Schedule hourly during waking hours:
- macOS: launchd plist (`StartCalendarInterval`), Windows: Task Scheduler, Linux: systemd timer/cron.
- PID-file mutual exclusion (overlapping runs corrupt half-written JSON).
- Each step fail-soft with a logged warning — one broken step must not kill the loop.
- Battery gate optional: skip heavy steps below 30% unplugged.

## Honest AI-cost tracking (if they use Claude Code / Codex)

The spend card and any cost dashboards. Two bugs WILL silently corrupt this if you skip them (both shipped in the original and survived for weeks):

1. **Transcript dedupe:** agent-CLI transcripts (`~/.claude/projects/**/*.jsonl`) write one line PER CONTENT BLOCK, each carrying the SAME full usage snapshot for the message. **Dedupe assistant usage by `message.id`** before summing — summing every line inflates tokens/cost 2–3×. Verify with an independent 20-line parser on one real transcript before trusting your numbers.
2. **Pricing table completeness:** maintain `{model-family → in/out/cache_read/cache_write per MTok}` with: an exact-ID table (so legacy models keep their era pricing), a family-substring fallback (so a NEW variant of a known family never prices as a different family), and an explicit unknown→$0-but-LOGGED policy. **When a new model family appears, it matches nothing and silently bills $0** — add a self-test assertion that the model id of the most recent transcript resolves to a price. Pull current rates from the provider's docs at build time; never from the model's memory.
3. Aggregate to `_agent_state/usage/stats.json` (`all_time` + `by_day` + `by_model`, same key schema), incremental on session-end hook + a full `recompute` script for corrections. Consistency invariant (assert in self-test): Σby_day == Σby_model == all_time on every metric.
4. Costs are API-equivalent value if the user is on subscription auth — label them as a usage meter, not an invoice.

## Session capture (memory that compounds)

On agent-session end (Claude Code hooks / wrapper script): parse the transcript → append learnings to `_agent_state/<agent>/memory.json` (`recent_learnings` ring ≤50, each ≤25 words), update usage stats, optionally extract a daily-note line. Exclude private dirs by path pattern. This is what makes month-2 of the system smarter than week-1.

## Recall index (for the voice brain + secret doors)

Python daemon: qdrant-local + fastembed (all-MiniLM), HTTP on localhost (`/retrieve`, `/health`, `/reindex`). Index the human-nav dirs; chunk ~500 tokens. Schedule re-index in the refresh loop. The daemon holds the DB lock for life — CLI fallback paths must detect lock-held and route through HTTP, not fight it.

## Triage gate (if running writing agents)

`triage.py` on the refresh loop: scan `Inbox/from-agents/*/`, validate frontmatter contract, then promote/hold by confidence with a **seen-set on (content_hash, run_id)** — without it, agents that re-emit the same file get re-triaged forever and every counter in the system lies. Append every verdict to a triage log (`- <ISO>: <VERDICT> <agent>/<file> …`) — Marble Run renders it, and it's your audit trail.
