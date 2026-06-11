#!/usr/bin/env bash
# brain-refresh.sh — keep the AI Second Brain fresh in the background.
# Invoked by ~/Library/LaunchAgents/com.tony.ai-brain-refresh.plist hourly during work hours.
# Safe to run by hand: `bash 99_Meta/brain-refresh.sh`
#
# Steps (each step failing does NOT abort the next — partial freshness > nothing):
#   1. Incremental SBAP rebuild  (build_brain_index.py)
#   2. SBAP endpoint regen        (build_brain_api.py)
#   3. Reconcile + triage Dust    (scaffold_dust_agents.py + triage_dust_writes.py)
#   4. Ingest AI sessions         (ingest_ai_sessions.py)
#   5. Skills Atlas regen         (generate-skills-atlas.py)
#   6. Convert binary docs → md   (convert_docs_to_md.py — incremental, time-budgeted)
#   7. Graphify incremental update
#   8. Recompute Claude stats      (recompute_claude_stats.py — true spend from transcripts)
#   9. Semantic recall index      (build_recall_index.py --once-if-free — incremental, opportunistic)
#  10. Daily Brief / Dashboard    (build_dashboard.py)
#  11. Account dashboards         (build_account_dashboards.py)
#  12. Self-promote loop          (self_promote.py — scan patterns + skills, emit HELD drafts only)
#  13. Pre-RFP capture radar      (prerfp_radar.py — weekly throttle; capture plans + Important/ nudges)
#
# Concurrency: flock on a pidfile prevents overlapping runs (no clogging if the
# Mac wakes from sleep and fires several scheduled invocations at once).

set -uo pipefail

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
LOG_DIR="$HOME/AI-Brain-build/logs"
LOCKFILE="$LOG_DIR/brain-refresh.pid"
LOG="$LOG_DIR/refresh-$(date +%F).log"

mkdir -p "$LOG_DIR"

# Single-flight: PID-based lock (macOS has no flock). Self-heals if a previous
# run crashed and left a stale lockfile.
if [[ -f "$LOCKFILE" ]]; then
  OLDPID=$(cat "$LOCKFILE" 2>/dev/null)
  if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
    printf '[%s] another refresh in progress (pid %s) — skipping\n' "$(date -Iseconds)" "$OLDPID" >> "$LOG"
    exit 0
  fi
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Augment PATH so launchd / hook contexts can find uv-installed graphify and
# Homebrew tools. Idempotent — duplicates are harmless.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

cd "$VAULT" || { echo "FATAL: vault not found at $VAULT" >> "$LOG"; exit 1; }

# Global kill switch (99_Meta/fleet-pause.sh): if paused, skip the whole refresh.
if [[ -f "$VAULT/_agent_state/AUTOMATION_PAUSED" ]]; then
  printf '[%s] FLEET PAUSED (AUTOMATION_PAUSED present) — skipping refresh\n' "$(date -Iseconds)" >> "$LOG"
  exit 0
fi

# Pick a usable timeout implementation: macOS has neither timeout nor gtimeout
# in /usr/bin; Homebrew coreutils installs `gtimeout`. Fall through to no-timeout
# if nothing is available.
TIMEOUT_BIN=""
if command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout"
elif command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout"
fi
nice_run() {
  local secs="$1"; shift
  if [[ -n "$TIMEOUT_BIN" ]]; then
    nice -n 10 "$TIMEOUT_BIN" "$secs" "$@"
  else
    nice -n 10 "$@"
  fi
}

{
  printf '\n==== %s — brain-refresh.sh start ====\n' "$(date -Iseconds)"
  printf 'timeout impl: %s\n' "${TIMEOUT_BIN:-none (commands will run without timeout)}"

  printf '[1/9] build_brain_index.py (incremental — default; pass --full to force a full rebuild)\n'
  nice_run 180 python3 "$VAULT"/build/tools/build_brain_index.py \
    || printf '  ⚠ step 1 failed (exit %s)\n' "$?"

  printf '[2/9] build_brain_api.py\n'
  nice_run 180 python3 "$VAULT"/build/tools/build_brain_api.py \
    || printf '  ⚠ step 2 failed (exit %s)\n' "$?"

  # Phase 2 (outcome-ledger spine): fold _agent_state/canonical source + outcome
  # ledger → _brain_api/canonical view with computed performance{}. MUST run AFTER
  # build_brain_api.py (which scaffolds the empty _brain_api/canonical dirs first).
  printf '[2b/9] build_canonical_view.py (fold outcome ledger → canonical view)\n'
  nice_run 60 python3 "$VAULT"/build/tools/build_canonical_view.py \
    || printf '  ⚠ step 2b failed (exit %s)\n' "$?"

  # Phantom Files: diff each open bid's folder against the winning-skeleton
  # doctrine (99_Meta/winning-skeleton.yaml) → _brain_api/bid/<id>/phantoms.json
  # (ghost rows for the explorer). MUST run AFTER build_brain_api.py, which
  # regenerates bid/_open.json and the per-bid dirs it writes into.
  printf '[2c/9] build_phantom_manifest.py (winning-skeleton diff → phantoms.json)\n'
  nice_run 60 python3 "$VAULT"/build/tools/build_phantom_manifest.py \
    || printf '  ⚠ step 2c failed (exit %s)\n' "$?"

  printf '[3/9] scaffold_dust_agents.py + triage_dust_writes.py (wire fleet, process writes)\n'
  if [[ -f "$VAULT/build/tools/scaffold_dust_agents.py" ]]; then
    nice_run 30 python3 "$VAULT/build/tools/scaffold_dust_agents.py" \
      || printf '  ⚠ scaffold reconcile failed (exit %s)\n' "$?"
  fi
  nice_run 60 python3 "$VAULT"/build/tools/triage_dust_writes.py \
    || printf '  ⚠ step 3 failed (exit %s)\n' "$?"
  # Self-heal/self-learn loop closer: escalate persistent dashboard issues + roll up usage.
  if [[ -f "$VAULT/build/tools/plugin_health_loop.py" ]]; then
    nice_run 20 python3 "$VAULT/build/tools/plugin_health_loop.py" \
      || printf '  ⚠ plugin-health-loop failed (exit %s)\n' "$?"
  fi

  printf '[4/9] ingest_ai_sessions.py (codex/gemini/dust session notes)\n'
  if [[ -f "$VAULT/build/tools/ingest_ai_sessions.py" ]]; then
    nice_run 60 python3 "$VAULT/build/tools/ingest_ai_sessions.py" \
      || printf '  ⚠ step 4 failed (exit %s)\n' "$?"
  else
    printf '  ingest_ai_sessions.py missing — skipping\n'
  fi

  printf '[5/9] generate-skills-atlas.py (Skills Atlas note)\n'
  nice_run 30 python3 "$VAULT/99_Meta/generate-skills-atlas.py" \
    || printf '  ⚠ step 5 failed (exit %s)\n' "$?"

  printf '[6/9] convert_docs_to_md.py (binary docs → Document Library, incremental)\n'
  if [[ -f "$VAULT/build/tools/convert_docs_to_md.py" ]]; then
    # Incremental + time-budgeted: only new/changed docs, capped so hourly runs stay cheap.
    nice_run 240 python3 "$VAULT/build/tools/convert_docs_to_md.py" --max-seconds 180 --quiet \
      || printf '  ⚠ step 6 failed (exit %s)\n' "$?"
  else
    printf '  convert_docs_to_md.py missing — skipping\n'
  fi

  printf '[7/9] graphify update .\n'
  if command -v graphify >/dev/null 2>&1; then
    nice_run 300 graphify update . \
      || printf '  ⚠ step 7 failed (exit %s)\n' "$?"
  else
    printf '  graphify CLI not on PATH — skipping (run /graphify in Claude Code once to install)\n'
  fi

  printf '[8/12] recompute_claude_stats.py (true spend from ~/.claude/projects transcripts)\n'
  if [[ -f "$VAULT/build/tools/recompute_claude_stats.py" ]]; then
    nice_run 120 python3 "$VAULT/build/tools/recompute_claude_stats.py" \
      || printf '  ⚠ step 8 failed (exit %s)\n' "$?"
  else
    printf '  recompute_claude_stats.py missing — skipping\n'
  fi

  printf '[9/12] semantic recall index (stage, then daemon-aware reindex)\n'
  # ULT-G2: stage FIRST, in THIS Apple-bash context (TCC-granted). The daemon is
  # uv-python — its own rsync from CloudStorage always dies rc=23, so it now only
  # indexes the staging mirror that we refresh here every hour.
  nice_run 600 bash "$HOME/AI-Brain-build/scripts/recall-index.sh" --stage-only \
    || printf '  ⚠ staging failed (exit %s) — daemon would reindex a stale mirror\n' "$?"
  # B1: if the warm recall daemon is running it owns the qdrant lock.
  # Trigger reindex via its /reindex endpoint instead of running the indexer
  # directly (which would fail to open the locked DB).
  DAEMON_URL="http://127.0.0.1:7766"
  if curl -sf -m 5 "$DAEMON_URL/health" > /dev/null 2>&1; then
    printf '  recall-daemon healthy — POST /reindex\n'
    # -m 1900: the first post-alignment reindex re-embeds ~1300 never-indexed files;
    # subsequent incremental runs are seconds.
    REINDEX_OUT=$(curl -sf -m 1900 -X POST "$DAEMON_URL/reindex" 2>&1) \
      && printf '  reindex via daemon: %s\n' "$REINDEX_OUT" \
      || printf '  ⚠ daemon /reindex failed — continuing (daemon still owns the lock)\n'
  else
    printf '  recall-daemon not running — running build_recall_index.py directly\n'
    nice_run 1800 bash "$HOME/AI-Brain-build/scripts/recall-index.sh" --once-if-free \
      || printf '  ⚠ step 9 skipped or failed (exit %s — DB locked or collection missing is OK)\n' "$?"
  fi

  # Daily_Brief.md is regenerated only once per day; Dashboard.md every refresh.
  DAILY_FLAG=""
  DAILY_BRIEF="$VAULT/02_Areas/Daily_Brief.md"
  if [[ ! -f "$DAILY_BRIEF" ]] || [[ "$(stat -f %Sm -t %F "$DAILY_BRIEF" 2>/dev/null)" != "$(date +%F)" ]]; then
    DAILY_FLAG="--daily"
  fi

  # Trust ledger: recompute per-agent R/θ from writes.jsonl so the dashboard's
  # trust leaderboard is fresh. Cheap (local JSON math, no LLM).
  printf '[9b/12] reputation.py --all (trust ledger refresh)\n'
  if [[ -f "$VAULT/build/tools/reputation.py" ]]; then
    nice_run 30 python3 "$VAULT/build/tools/reputation.py" --all > /dev/null \
      || printf '  ⚠ step 9b failed (exit %s)\n' "$?"
  fi

  # Ghost forecaster: once per day, score yesterday's claims then emit tomorrow's
  # (claude -p haiku, bounded; fail-soft — never blocks the dashboard).
  GHOST_MARKER="$VAULT/_brain_api/ghost/.last-run-date"
  if [[ "$(cat "$GHOST_MARKER" 2>/dev/null)" != "$(date +%F)" ]]; then
    printf '[9c/12] ghost forecaster (score yesterday, predict tomorrow)\n'
    if [[ -f "$VAULT/build/tools/ghost_forecast.py" ]]; then
      nice_run 120 python3 "$VAULT/build/tools/ghost_forecast.py" --score \
        || printf '  ⚠ ghost scorer failed (exit %s)\n' "$?"
      nice_run 240 python3 "$VAULT/build/tools/ghost_forecast.py" \
        && { mkdir -p "$(dirname "$GHOST_MARKER")"; date +%F > "$GHOST_MARKER"; } \
        || printf '  ⚠ ghost forecast failed (exit %s)\n' "$?"
    fi
  fi

  printf '[10/12] build_dashboard.py%s\n' "${DAILY_FLAG:+ $DAILY_FLAG}"
  if [[ -f "$VAULT/build/tools/build_dashboard.py" ]]; then
    nice_run 60 python3 "$VAULT/build/tools/build_dashboard.py" $DAILY_FLAG \
      || printf '  ⚠ step 10 failed (exit %s)\n' "$?"
  else
    printf '  build_dashboard.py missing — skipping\n'
  fi

  printf '[11/12] build_account_dashboards.py (per-account dashboards)\n'
  if [[ -f "$VAULT/build/tools/build_account_dashboards.py" ]]; then
    nice_run 60 python3 "$VAULT/build/tools/build_account_dashboards.py" \
      || printf '  ⚠ step 11 failed (exit %s)\n' "$?"
  else
    printf '  build_account_dashboards.py missing — skipping\n'
  fi

  # Step 12: self-promote loop — read-only scan of agent patterns + skill corrections.
  # Emits HELD draft proposals to 00_Inbox/from-dust/self-promote/ for Tony's review.
  # Never auto-applies anything. Safe to run every refresh cycle.
  printf '[12/12] self_promote.py (scan patterns+skills, emit HELD draft proposals)\n'
  if [[ -f "$VAULT/build/tools/self_promote.py" ]]; then
    nice_run 30 python3 "$VAULT/build/tools/self_promote.py" --quiet \
      || printf '  ⚠ step 12 failed (exit %s)\n' "$?"
  else
    printf '  self_promote.py missing — skipping\n'
  fi

  # Step 13: pre-RFP capture radar (HS-19) — weekly throttle on memory.json mtime.
  # Scores clients for RFP-likelihood from intel-agent writes + account notes; on
  # trigger writes 02_Areas/Accounts/<client>/capture-<client>.md + an Important/
  # nudge. Deterministic under cron (--no-llm); re-run by hand without the flag
  # for LLM-written capture-plan copy.
  printf '[13/13] prerfp_radar.py (pre-RFP radar — weekly throttle)\n'
  RADAR_STATE="$VAULT/_agent_state/prerfp-radar/memory.json"
  if [[ -f "$VAULT/build/tools/prerfp_radar.py" ]]; then
    if [[ -f "$RADAR_STATE" && -n "$(find "$RADAR_STATE" -mtime -6 2>/dev/null)" ]]; then
      printf '  radar ran <6d ago — skipping\n'
    else
      nice_run 120 python3 "$VAULT/build/tools/prerfp_radar.py" --no-llm \
        || printf '  ⚠ step 13 failed (exit %s)\n' "$?"
    fi
  else
    printf '  prerfp_radar.py missing — skipping\n'
  fi

  # Step 14: promise ledger (ADHD-3 A) — daily throttle. Extracts commitments
  # from meeting notes (LLM haiku; conservative, >=0.80 gate), reconciles
  # kept/stale/broken, writes _brain_api/promises/{ledger,held,summary}.json.
  printf '[14/16] promise_ledger.py (daily throttle)\n'
  PROMISE_STATE="$VAULT/_agent_state/promise-ledger/memory.json"
  if [[ -f "$VAULT/build/tools/promise_ledger.py" ]]; then
    if [[ -f "$PROMISE_STATE" && -n "$(find "$PROMISE_STATE" -mmin -1200 2>/dev/null)" ]]; then
      printf '  promise ledger ran <20h ago — skipping\n'
    else
      nice_run 300 python3 "$VAULT/build/tools/promise_ledger.py" --limit 20 \
        || printf '  ⚠ step 14 failed (exit %s)\n' "$?"
    fi
  else
    printf '  promise_ledger.py missing — skipping\n'
  fi

  # Step 15: cross-agent contradiction detector (ADHD-3 B) — every refresh
  # (deterministic, cheap). HIGH contradictions escalate to Important/escalations/.
  printf '[15/16] contradiction_detector.py\n'
  if [[ -f "$VAULT/build/tools/contradiction_detector.py" ]]; then
    nice_run 60 python3 "$VAULT/build/tools/contradiction_detector.py" \
      || printf '  ⚠ step 15 failed (exit %s)\n' "$?"
  else
    printf '  contradiction_detector.py missing — skipping\n'
  fi

  # Step 16: deal microstructure tape (ADHD-3 C) — per open bid, deterministic
  # under cron (--no-llm; run by hand without the flag for LLM extraction).
  # Widening trend on a Propose/Negotiate bid escalates.
  printf '[16/16] deal_tape.py (open bids, --no-llm)\n'
  if [[ -f "$VAULT/build/tools/deal_tape.py" ]]; then
    python3 - "$VAULT" <<'PYEOF' 2>/dev/null | while IFS=$'\t' read -r BID CLIENT; do
import json,sys
try:
    d=json.load(open(sys.argv[1]+"/_brain_api/bid/_open.json"))
    for b in (d.get("bids") or d.get("data") or []):
        bid=b.get("bid_id") or b.get("id"); cl=b.get("client","")
        if bid and cl: print(f"{bid}\t{cl}")
except Exception: pass
PYEOF
      nice_run 120 python3 "$VAULT/build/tools/deal_tape.py" --bid "$BID" --client "$CLIENT" --no-llm \
        || printf '  ⚠ step 16 failed for %s (exit %s)\n' "$BID" "$?"
    done
  else
    printf '  deal_tape.py missing — skipping\n'
  fi

  printf '==== %s — brain-refresh.sh end (16 steps) ====\n' "$(date -Iseconds)"
} >> "$LOG" 2>&1
