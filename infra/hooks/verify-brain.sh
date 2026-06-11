#!/usr/bin/env bash

# Ultron voice turns must stay snappy: skip all brain-brief work for those sessions.
[[ "${ULTRON_VOICE:-0}" == "1" ]] && exit 0
# verify-brain.sh — auto-context + freshness + self-healing for Tony's AI Second Brain.
# Called by Claude Code hooks in ~/.claude/settings.json.
#
# Modes:
#   --session-start    rich brief; fires self-healing async if STALE / graphify CLI missing
#   --prompt           JSON hookSpecificOutput; freshness ping + prompt-keyword prefetch
#   --pre-read         JSON hookSpecificOutput; injects matching SBAP endpoint for vault Read
#   --status           full health check (manual / future /brain-status)
#
# Exit code is always 0 — hook output is informational, never blocking.
# Silence everything: export VAULT_BRAIN_QUIET=1.
# Uses macOS stat (%m). PID-based throttle markers in ~/AI-Brain-build/.
#
# Hot-path budget: --prompt and --pre-read must stay <200ms (run on every prompt/Read).

set -uo pipefail

[[ "${VAULT_BRAIN_QUIET:-0}" == "1" ]] && exit 0

MODE="${1:---status}"
VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
STALE_THRESHOLD=7200   # 2h — older → STALE
MARKER_DIR="$VAULT/99_Meta/.markers"
HEAL_LOG="$MARKER_DIR/self-heal.log"

# Scope guard: hook modes run only inside the vault (or with VAULT_BRAIN_ANYWHERE=1).
case "$PWD" in
  "$VAULT"*) ;;
  *)
    [[ "${VAULT_BRAIN_ANYWHERE:-0}" == "1" || "$MODE" == "--status" ]] || exit 0
    ;;
esac

# ──────────────────────────── helpers ────────────────────────────

mtime_age() {
  local f="$1"
  [[ ! -e "$f" ]] && { echo "missing"; return; }
  local now=$(date +%s)
  local m; m=$(stat -f %m "$f" 2>/dev/null) || { echo "missing"; return; }
  local age=$(( now - m ))
  if   (( age < 60 ));    then echo "${age}s"
  elif (( age < 3600 ));  then echo "$((age/60))m"
  elif (( age < 86400 )); then echo "$((age/3600))h"
  else                          echo "$((age/86400))d"
  fi
}

is_stale() {
  local f="$1" threshold="$2"
  [[ ! -e "$f" ]] && return 1
  local now=$(date +%s)
  local m; m=$(stat -f %m "$f" 2>/dev/null) || return 1
  (( now - m > threshold ))
}

# True if marker exists and is younger than threshold seconds.
is_throttled() {
  local marker="$1" threshold="$2"
  [[ ! -f "$marker" ]] && return 1
  local now=$(date +%s)
  local m; m=$(stat -f %m "$marker" 2>/dev/null) || return 1
  (( now - m < threshold ))
}

# Detached background command — survives parent exit, no zombie processes.
fire_async() {
  local logfile="$1"; shift
  (nohup bash -c "$*" >> "$logfile" 2>&1 < /dev/null &) >/dev/null 2>&1
}

# Emit empty JSON hookSpecificOutput (no-op for the model).
emit_no_op() { printf '{}\n'; }

# ─────────────────────────── freshness ───────────────────────────

GRAPH="$VAULT/graphify-out/graph.json"
API_MANIFEST="$VAULT/_brain_api/_manifest.json"
INDEX_META="$VAULT/_brain_index/_meta.json"
OPEN_BIDS="$VAULT/_brain_api/bid/_open.json"
REGISTRY="$VAULT/_agent_state/_registry.json"

GRAPH_AGE=$(mtime_age "$GRAPH")
API_AGE=$(mtime_age "$API_MANIFEST")
INDEX_AGE=$(mtime_age "$INDEX_META")

WARN=""
for f in "$GRAPH" "$API_MANIFEST" "$INDEX_META"; do
  if is_stale "$f" "$STALE_THRESHOLD"; then WARN="STALE"; break; fi
done
for f in "$API_MANIFEST"; do
  [[ ! -e "$f" ]] && WARN="MISSING"
done

# ─────────────────────────── self-healing ────────────────────────

self_heal() {
  local healed=""
  mkdir -p "$(dirname "$HEAL_LOG")"

  # Ensure ~/.local/bin and Homebrew paths are visible so uv/pipx are findable
  # when this runs under launchd or other limited-PATH contexts.
  local PATH_AUG="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

  if ! PATH="$PATH_AUG" command -v graphify >/dev/null 2>&1; then
    local marker="$MARKER_DIR/.last_graphify_install_attempt"
    if ! is_throttled "$marker" 86400; then
      touch "$marker"
      # Use a heredoc-style command so $(date) is expanded by the async bash, not the parent.
      fire_async "$HEAL_LOG" 'export PATH="'"$PATH_AUG"'"; printf "\n== %s graphify install ==\n" "$(date -Iseconds)"; if command -v uv >/dev/null 2>&1; then uv tool install --upgrade graphifyy; elif command -v pipx >/dev/null 2>&1; then pipx install graphifyy; else python3 -m pip install --user graphifyy 2>&1 || python3 -m pip install --break-system-packages graphifyy 2>&1; fi'
      healed+="graphify CLI missing → install fired async (log: ${HEAL_LOG/$HOME/~}). "
    fi
  fi

  if [[ -n "$WARN" ]]; then
    local marker="$MARKER_DIR/.last_refresh_attempt"
    if ! is_throttled "$marker" 1800; then
      touch "$marker"
      fire_async "$HEAL_LOG" 'export PATH="'"$PATH_AUG"'"; printf "\n== %s brain-refresh ==\n" "$(date -Iseconds)"; bash "'"$VAULT"'/99_Meta/brain-refresh.sh"'
      healed+="${WARN} → brain-refresh fired async. Next session will be fresh. "
    fi
  fi

  [[ -n "$healed" ]] && echo "↻ Self-heal: $healed"
}

# ─────────────────────────── context loaders ─────────────────────

daily_journal() {
  local today; today=$(date +%F)
  local f="$VAULT/02_Areas/Daily/$today.md"
  [[ -f "$f" ]] || return 0
  printf '\n── Today\047s journal (02_Areas/Daily/%s.md) ──\n' "$today"
  head -100 "$f"
}

recent_vault_changes() {
  # Files modified in the last 24h, outside generated dirs.
  local found
  found=$(find "$VAULT" -type f -name '*.md' -mtime -1 \
            -not -path "*/graphify-out/*" \
            -not -path "*/_brain_index/*" \
            -not -path "*/_brain_api/*" \
            -not -path "*/.obsidian/*" \
            -not -path "*/04_Archives/*" \
            -not -path "*/_External/*" 2>/dev/null \
          | head -10)
  [[ -z "$found" ]] && return 0
  printf '\n── Vault changes (last 24h, top 10) ──\n'
  echo "$found" | while read -r f; do
    printf '  %s  %s\n' "$(stat -f '%Sm' -t '%m-%d %H:%M' "$f")" "${f#$VAULT/}"
  done
}

recent_git_commits() {
  local log
  log=$(git -C "$VAULT" log --oneline -5 2>/dev/null) || return 0
  [[ -z "$log" ]] && return 0
  printf '\n── Recent vault commits (last 5) ──\n'
  echo "$log" | sed 's/^/  /'
}

pending_agent_writes() {
  local writes_dir="$VAULT/00_Inbox/from-dust"
  [[ ! -d "$writes_dir" ]] && return 0
  local files; files=$(find "$writes_dir" -type f -name '*.md' 2>/dev/null)
  [[ -z "$files" ]] && return 0
  printf '\n── Pending SBAP writes awaiting triage (00_Inbox/from-dust/) ──\n'
  echo "$files" | while read -r f; do
    printf '  %s\n' "${f#$VAULT/}"
  done
}

open_bids_block() {
  [[ ! -f "$OPEN_BIDS" ]] && return 0
  local count; count=$(jq -r '.bids | length' "$OPEN_BIDS" 2>/dev/null || echo 0)
  if (( count == 0 )); then
    printf 'Open bids: 0 (no active opportunities in _brain_api/bid/_open.json)\n'
  else
    printf '\n── Open bids (%s) ──\n' "$count"
    jq -r '.bids | sort_by(.deadline // "9999-12-31")[] |
      "  \(.bid_id // .name // "?")  stage=\(.stage // "?")  due \(.deadline // "?")  client=\(.client // "?")"' \
      "$OPEN_BIDS" 2>/dev/null | head -20
  fi
}

# Promise ledger pulse — 3 lines max from _brain_api/promises/summary.json
# (built by build/tools/promise_ledger.py, brain-refresh step 14).
promises_block() {
  local PSUM="$VAULT/_brain_api/promises/summary.json"
  [[ ! -f "$PSUM" ]] && return 0
  local due overdue
  due=$(jq -r '.due_48h | length' "$PSUM" 2>/dev/null || echo 0)
  overdue=$(jq -r '.overdue_to_owner | length' "$PSUM" 2>/dev/null || echo 0)
  (( due == 0 && overdue == 0 )) && return 0
  printf '\n── Promises (ledger) ──\n'
  (( due > 0 )) && jq -r '.due_48h[:2][] | "  ⏳ you owe: \(.text[:90]) (due \(.due // "?"))"' "$PSUM" 2>/dev/null
  (( overdue > 0 )) && jq -r '.overdue_to_owner[:2][] | "  🔔 owed to you: \(.text[:90]) (due \(.due // "?"))"' "$PSUM" 2>/dev/null
}

# Build account+bid keyword list once; cache for hot-path reuse.
known_keywords() {
  local cache="$MARKER_DIR/.brain_keywords"
  if [[ -f "$cache" ]] && is_throttled "$cache" 300; then
    cat "$cache"; return
  fi
  mkdir -p "$MARKER_DIR"
  {
    [[ -d "$VAULT/02_Areas/Accounts" ]] && find "$VAULT/02_Areas/Accounts" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r d; do
      basename "$d"
    done
    [[ -d "$VAULT/RFPs" ]] && find "$VAULT/RFPs" -name "00 - Brief.md" 2>/dev/null | while read -r b; do
      d="$(dirname "$b")"; case "$d" in */_*) continue;; esac; basename "$d"
    done
  } | grep -v '^_template$' > "$cache" 2>/dev/null || true
  cat "$cache"
}

# Match prompt text against known account/bid names; emit any matching SBAP briefs.
prefetch_for_prompt() {
  local prompt="$1"
  [[ -z "$prompt" ]] && return 0
  local injected=""
  while IFS= read -r kw; do
    [[ -z "$kw" ]] && continue
    if echo "$prompt" | grep -qiF "$kw"; then
      local acct="$VAULT/_brain_api/account/$kw/brief.json"
      local bid="$VAULT/_brain_api/bid/$kw/status.json"
      if [[ -f "$acct" ]]; then
        injected+=$'\n── Auto-loaded account brief: '"$kw"$' ──\n'
        injected+=$(cat "$acct")
        injected+=$'\n'
      fi
      if [[ -f "$bid" ]]; then
        injected+=$'\n── Auto-loaded bid status: '"$kw"$' ──\n'
        injected+=$(cat "$bid")
        injected+=$'\n'
      fi
    fi
  done < <(known_keywords)
  printf '%s' "$injected"
}

# Map a vault file path to its matching SBAP endpoint content, if any.
sbap_endpoint_for_path() {
  local fp="$1"
  case "$fp" in
    "$VAULT"/*) ;;
    *) return 0 ;;
  esac
  local rel="${fp#$VAULT/}"
  case "$rel" in
    RFPs/*)
      local bid; bid=$(echo "$rel" | cut -d/ -f2)
      local f="$VAULT/_brain_api/bid/$bid/status.json"
      [[ -f "$f" ]] && { printf 'SBAP bid status for %s:\n' "$bid"; cat "$f"; }
      ;;
    02_Areas/Accounts/*)
      local acct; acct=$(echo "$rel" | cut -d/ -f3)
      local f="$VAULT/_brain_api/account/$acct/brief.json"
      [[ -f "$f" ]] && { printf 'SBAP account brief for %s:\n' "$acct"; cat "$f"; }
      ;;
    03_Resources/PowerPoint*Standards/*|03_Resources/Templates/*)
      local f="$VAULT/_brain_api/canonical/exec_summary"
      [[ -d "$f" ]] && printf 'SBAP canonical blocks available at: _brain_api/canonical/\n'
      ;;
  esac
}

# ─────────────────────── stale-agent heartbeat ───────────────────
# Check each ACTIVE agent with an expected_cadence_hours.
# If (now - last_active) > cadence → emit STALE-AGENT warning and, throttled once
# per day, drop an escalation file into Important/escalations/ via the SBAP inbox.
#
# Throttle: one escalation file per agent per day (marker: $MARKER_DIR/.stale-<agent>)
#
check_stale_agents() {
  [[ ! -f "$REGISTRY" ]] && return 0
  command -v jq >/dev/null 2>&1 || return 0

  local now; now=$(date +%s)
  local stale_found=""
  local ESC_DIR="$VAULT/00_Inbox/from-dust/incident-commander"
  mkdir -p "$ESC_DIR"
  # YAML front-matter delimiters written via variable to avoid any shell/sandbox stripping.
  local FM="---"

  # Read active agents with a cadence set.
  # Use select(.expected_cadence_hours) — truthy check — to avoid != which the sandbox escapes.
  local agents_json
  agents_json=$(jq -c '.agents[] | select(.status == "active") | select(.expected_cadence_hours) | {name: .agent_name, cadence: .expected_cadence_hours}' "$REGISTRY" 2>/dev/null) || return 0

  while IFS= read -r rec; do
    [[ -z "$rec" ]] && continue
    local name cadence
    name=$(printf '%s' "$rec" | jq -r '.name')
    cadence=$(printf '%s' "$rec" | jq -r '.cadence')

    # Resolve last_active from stats.json
    local stats_file="$VAULT/_agent_state/$name/stats.json"
    local last_active_ts=""
    if [[ -f "$stats_file" ]]; then
      last_active_ts=$(jq -r '.last_active // empty' "$stats_file" 2>/dev/null || true)
    fi

    # If no stats.json / no last_active, treat as never active (use epoch 0)
    local last_epoch=0
    if [[ -n "$last_active_ts" ]]; then
      # ISO 8601 → epoch (macOS date)
      local ts_clean="${last_active_ts%.*}"  # drop sub-seconds
      ts_clean="${ts_clean%+*}"             # drop tz offset if present
      ts_clean="${ts_clean%Z}"              # drop Z
      last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$ts_clean" "+%s" 2>/dev/null || echo 0)
    fi

    local silent_s=$(( now - last_epoch ))
    local cadence_s=$(( cadence * 3600 ))
    if (( silent_s > cadence_s )); then
      local silent_h=$(( silent_s / 3600 ))
      stale_found+="⚠ STALE-AGENT: $name silent ${silent_h}h (expected every ${cadence}h)\n"

      # Escalation: throttled to once per day per agent
      local marker="$MARKER_DIR/.stale-${name}-esc"
      if ! is_throttled "$marker" 86400; then
        touch "$marker"
        local date_iso; date_iso=$(date +%F)
        local ts_iso; ts_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        local esc_slug="${date_iso}-stale-agent-${name}.md"
        {
          printf '%s\n' "$FM"
          printf 'sbap_version: "1.0"\n'
          printf 'source_agent: incident-commander\n'
          printf 'source_run_id: "verify-brain-stale-%s-%s"\n' "$name" "$ts_iso"
          printf 'generated: "%s"\n' "$ts_iso"
          printf 'input_context_refs:\n'
          printf '  - "_agent_state/_registry.json"\n'
          printf '  - "_agent_state/%s/stats.json"\n' "$name"
          printf 'output_type: escalation_alert\n'
          printf 'target_path: "Important/escalations/%s"\n' "$esc_slug"
          printf 'confidence: 0.95\n'
          printf 'sensitivity: internal\n'
          printf 'needs_review: false\n'
          printf 'reasoning_summary: |\n'
          printf '  Agent %s has been silent for %sh (expected every %sh).\n' "$name" "$silent_h" "$cadence"
          printf '%s\n\n' "$FM"
          printf '# STALE-AGENT: %s — silent %sh\n\n' "$name" "$silent_h"
          printf '**Date:** %s\n\n' "$date_iso"
          printf '| Field | Value |\n'
          printf '|---|---|\n'
          printf '| Agent | `%s` |\n' "$name"
          printf '| Silent for | %sh |\n' "$silent_h"
          printf '| Expected cadence | every %sh |\n' "$cadence"
          printf '| Last active | %s |\n' "${last_active_ts:-never}"
          printf '| Stats file | `_agent_state/%s/stats.json` |\n\n' "$name"
          printf '## Actions\n\n'
          printf '- Check Dust for the agent '"'"'%s'"'"' — is it firing on schedule?\n' "$name"
          printf '- Inspect `_agent_state/%s/` for write failures or holds.\n' "$name"
          printf '- If the agent is decommissioned, set `status: inactive` in `_agent_state/_registry.json`.\n'
        } > "$ESC_DIR/$esc_slug"
      fi
    fi
  done <<< "$agents_json"

  [[ -n "$stale_found" ]] && printf '%b' "$stale_found"
}

# ─────────────────────────── modes ───────────────────────────────

REBUILD_HINT='cd "'"$VAULT"'" && bash 99_Meta/brain-refresh.sh'

case "$MODE" in
  --session-start)
    AGENT_TOTAL=0; AGENT_ACTIVE=0
    if [[ -f "$REGISTRY" ]]; then
      AGENT_TOTAL=$(jq -r '.agents | length' "$REGISTRY" 2>/dev/null || echo 0)
      AGENT_ACTIVE=$(jq -r '[.agents[] | select(.status=="active")] | length' "$REGISTRY" 2>/dev/null || echo 0)
    fi
    AGENT_OTHER=$(( AGENT_TOTAL - AGENT_ACTIVE ))

    printf '═══ AI Second Brain — auto-context ═══\n'
    [[ -n "$WARN" ]] && printf '⚠ %s — manual rebuild: %s\n' "$WARN" "$REBUILD_HINT"

    HEAL_MSG=$(self_heal)
    [[ -n "$HEAL_MSG" ]] && printf '%s\n' "$HEAL_MSG"

    printf 'Freshness: graph %s · _brain_api %s · _brain_index %s\n' "$GRAPH_AGE" "$API_AGE" "$INDEX_AGE"
    printf 'SBAP agents: %s/%s active (%s scaffolded — Dust wiring pending)\n' "$AGENT_ACTIVE" "$AGENT_TOTAL" "$AGENT_OTHER"
    printf 'Rule 1: /graphify query "<topic>" → _brain_api/<endpoint>.json → raw Read (last resort)\n'
    printf 'Pointers: 02_Areas/Pipeline.md · _brain_api/_manifest.json · 99_Meta/verify-brain.sh (silence: VAULT_BRAIN_QUIET=1)\n'
    open_bids_block
    promises_block
    daily_journal
    recent_vault_changes
    recent_git_commits
    pending_agent_writes

    # Heartbeat check: warn on STALE-AGENT(s) + throttled escalation write.
    check_stale_agents

    # Touch session marker for next-session "what changed since" calculations.
    mkdir -p "$MARKER_DIR"
    touch "$MARKER_DIR/.last_session_marker"
    ;;

  --prompt)
    PAYLOAD=$(cat 2>/dev/null || echo '{}')
    PROMPT=$(echo "$PAYLOAD" | jq -r '.prompt // ""' 2>/dev/null || echo "")

    if [[ -n "$WARN" ]]; then
      FRESH_LINE=$(printf 'Brain: ⚠%s (graph %s, api %s) — /graphify query first; rebuild if needed.' "$WARN" "$GRAPH_AGE" "$API_AGE")
    else
      FRESH_LINE=$(printf 'Brain: graph %s, api %s (fresh) — /graphify query before raw Read.' "$GRAPH_AGE" "$API_AGE")
    fi

    PREFETCHED=$(prefetch_for_prompt "$PROMPT")

    if [[ -n "$PREFETCHED" ]]; then
      jq -n --arg ctx "$FRESH_LINE$PREFETCHED" '{
        "hookSpecificOutput": { "hookEventName": "UserPromptSubmit", "additionalContext": $ctx }
      }'
    else
      printf '%s\n' "$FRESH_LINE"
    fi
    ;;

  --pre-read)
    PAYLOAD=$(cat 2>/dev/null || echo '{}')
    FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")
    SBAP=$(sbap_endpoint_for_path "$FILE_PATH")
    if [[ -n "$SBAP" ]]; then
      jq -n --arg ctx "Rule 1 reminder: SBAP endpoint exists for this path; auto-loaded below.\n$SBAP" '{
        "hookSpecificOutput": {
          "hookEventName": "PreToolUse",
          "permissionDecision": "allow",
          "additionalContext": $ctx
        }
      }'
    else
      emit_no_op
    fi
    ;;

  --status)
    AGENT_TOTAL=0; AGENT_ACTIVE=0
    if [[ -f "$REGISTRY" ]]; then
      AGENT_TOTAL=$(jq -r '.agents | length' "$REGISTRY" 2>/dev/null || echo 0)
      AGENT_ACTIVE=$(jq -r '[.agents[] | select(.status=="active")] | length' "$REGISTRY" 2>/dev/null || echo 0)
    fi
    AGENT_OTHER=$(( AGENT_TOTAL - AGENT_ACTIVE ))

    printf '═══ AI Second Brain — health check ═══\n'
    printf 'Vault: %s\n\n' "$VAULT"

    printf 'ARTIFACTS\n'
    printf '  graphify-out/graph.json       : %s\n' "$GRAPH_AGE"
    printf '  _brain_api/_manifest.json     : %s\n' "$API_AGE"
    printf '  _brain_index/_meta.json       : %s\n' "$INDEX_AGE"
    printf '  Stale threshold               : %sh\n' "$((STALE_THRESHOLD/3600))"
    printf '  Verdict                       : %s\n\n' "${WARN:-FRESH}"

    printf 'AGENTS\n'
    printf '  Registered: %s    Active: %s    Other: %s\n\n' "$AGENT_TOTAL" "$AGENT_ACTIVE" "$AGENT_OTHER"

    printf 'KEYWORDS CACHED FOR PROMPT-PREFETCH\n'
    local_kw=$(known_keywords)
    if [[ -z "$local_kw" ]]; then
      printf '  (none — populate RFPs/<bid>/ or 02_Areas/Accounts/<client>/)\n\n'
    else
      printf '%s\n\n' "$local_kw" | sed 's/^/  /'
    fi

    printf 'SELF-HEAL THROTTLES\n'
    printf '  Last graphify install attempt : %s\n' "$(mtime_age "$MARKER_DIR/.last_graphify_install_attempt")"
    printf '  Last brain-refresh attempt    : %s\n\n' "$(mtime_age "$MARKER_DIR/.last_refresh_attempt")"

    printf 'REBUILD COMMANDS\n'
    printf '  %s\n' "$REBUILD_HINT"
    printf '  /graphify update .   (or `graphify update .` if CLI on PATH)\n\n'

    printf 'SCHEDULED REFRESH\n'
    LAUNCHD_OUT=$(launchctl list 2>/dev/null | grep -E 'ai-brain|graphify' || true)
    if [[ -z "$LAUNCHD_OUT" ]]; then
      printf '  (no launchd job loaded — see ~/Library/LaunchAgents/com.tony.ai-brain-refresh.plist)\n\n'
    else
      printf '%s\n\n' "$LAUNCHD_OUT"
    fi

    printf 'RECENT SELF-HEAL LOG\n'
    if [[ -f "$HEAL_LOG" ]]; then
      tail -n 10 "$HEAL_LOG" | sed 's/^/  /'
    else
      printf '  (no self-heal log yet — fires lazily)\n'
    fi
    printf '\nRECENT REFRESH LOG\n'
    LATEST_LOG=$(find "$HOME/AI-Brain-build/logs" -name 'refresh-*.log' -type f 2>/dev/null | sort | tail -1)
    if [[ -n "$LATEST_LOG" && -f "$LATEST_LOG" ]]; then
      printf '  %s\n' "$LATEST_LOG"
      tail -n 8 "$LATEST_LOG" | sed 's/^/    /'
    else
      printf '  (no refresh log yet)\n'
    fi
    ;;

  *)
    printf 'verify-brain.sh: unknown mode "%s" (--session-start | --prompt | --pre-read | --status)\n' "$MODE" >&2
    ;;
esac

exit 0
