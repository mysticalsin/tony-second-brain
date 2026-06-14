#!/usr/bin/env bash
# preflight-md-write.sh — PreToolUse:Write|Edit hook.
# GATE (blocks): non-conforming SBAP writes BEFORE they land in 00_Inbox/from-dust/.
# Twin: validate-md-write.sh (PostToolUse audit — fires after write, never blocks).
#
# Hook input on stdin (JSON):
#   Write: { "tool_name": "Write", "tool_input": { "file_path": "...", "content": "..." } }
#   Edit:  { "tool_name": "Edit",  "tool_input": { "file_path": "...", "new_string": "...", ... } }
#
# Block convention (Claude Code PreToolUse):
#   - emit {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
#           "permissionDecisionReason":"<msg>"}} on stdout  AND  exit 2
#
# Pass: exit 0 (no stdout output required).
#
# Block conditions (all checked against content that is ABOUT TO BE written):
#   1. Missing any required SBAP frontmatter field
#   2. target_path contains an unresolved placeholder  ([TYPE_REQUIRED]/... or <any_slug>)
#   3. source_run_id is empty or blank
#   4. confidence >= agent's reputation theta (from _agent_state/<agent>/reputation.json,
#      fallback 0.85) AND input_context_refs is empty
#
# Pass conditions: content passes all four gates above, OR file is not under
#   00_Inbox/from-dust/ (hook is a no-op outside the SBAP write zone).
#
# Quiet mode: VAULT_BRAIN_QUIET=1 disables all output but still blocks on violations.

set -uo pipefail

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
VIOLATIONS_LOG="$VAULT/_agent_state/_conduct/conduct-violations.jsonl"

# ── Read stdin payload ──────────────────────────────────────────────────────
PAYLOAD=$(cat 2>/dev/null || echo '{}')

# ── Extract file path ───────────────────────────────────────────────────────
FILE=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null)

# ── Gate only .md writes under 00_Inbox/from-dust/ ─────────────────────────
case "$FILE" in
  "$VAULT/00_Inbox/from-dust/"*.md) ;;   # in scope — continue
  *) exit 0 ;;                            # out of scope — pass silently
esac

# ── Extract content to validate ─────────────────────────────────────────────
CONTENT=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    val = ti.get('content') or ti.get('new_string') or ''
    print(val)
except Exception:
    print('')
" 2>/dev/null)

# ── Helper: emit deny decision + log conduct violation ──────────────────────
deny() {
  local reason="$1" rule="${2:-§11 sbap-provenance}" severity="${3:-high}"
  local ts agent json_out

  agent=$(printf '%s' "$CONTENT" | grep -m1 '^source_agent:' \
    | sed 's/^source_agent:[[:space:]]*//; s/["'"'"']//g; s/[[:space:]]//g' 2>/dev/null || true)
  [[ -z "$agent" ]] && agent="unknown"

  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "1970-01-01T00:00:00Z")

  mkdir -p "$(dirname "$VIOLATIONS_LOG")" 2>/dev/null || true
  touch "$VIOLATIONS_LOG" 2>/dev/null || true
  printf '{"ts":"%s","source":"preflight-md-write","agent":"%s","rule":"%s","severity":"%s","detail":"%s","file":"%s"}\n' \
    "$ts" "$agent" "$rule" "$severity" \
    "$(printf '%s' "$reason" | sed 's/"/\\"/g')" \
    "$(printf '%s' "$FILE" | sed 's/"/\\"/g')" \
    >> "$VIOLATIONS_LOG" 2>/dev/null || true

  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' \
    "$(printf '%s' "preflight-md-write BLOCKED: $reason" | sed 's/"/\\"/g')"

  printf '[preflight-md-write] BLOCKED %s → %s\n' "$FILE" "$reason" >&2

  exit 2
}

# ── Parse the SBAP frontmatter from content ─────────────────────────────────
HAS_FM_SIGNALS=0
for _f in sbap_version source_agent source_run_id generated input_context_refs output_type target_path confidence; do
  if printf '%s' "$CONTENT" | grep -qE "^${_f}:"; then
    HAS_FM_SIGNALS=1
    break
  fi
done

if (( HAS_FM_SIGNALS == 0 )); then
  exit 0
fi

# ── Gate 1: Required SBAP fields ────────────────────────────────────────────
REQUIRED_FIELDS=(sbap_version source_agent source_run_id generated input_context_refs output_type target_path confidence)
missing=()
for field in "${REQUIRED_FIELDS[@]}"; do
  if ! printf '%s' "$CONTENT" | grep -qE "^${field}:"; then
    missing+=("$field")
  fi
done

if (( ${#missing[@]} > 0 )); then
  missing_str=$(IFS=,; printf '%s' "${missing[*]}")
  deny "missing required SBAP frontmatter fields: ${missing_str}" "§11 sbap-provenance" "high"
fi

# ── Gate 2: Unresolved placeholder in target_path ──────────────────────────
if printf '%s' "$CONTENT" | grep -qE '^target_path:.*(\[[A-Z_]+\]|<[a-z_A-Z_]+>|TYPE_REQUIRED)'; then
  deny "target_path contains an unresolved placeholder — agent must resolve [TYPE_REQUIRED] or <slug> before writing" \
    "§11 sbap-provenance" "high"
fi

# ── Gate 3: Empty source_run_id ─────────────────────────────────────────────
if printf '%s' "$CONTENT" | grep -qE "^source_run_id:[[:space:]]*(\"\")?('['])?[[:space:]]*$"; then
  deny "source_run_id is empty — every write must carry a unique invocation ID" \
    "§11 sbap-provenance" "high"
fi

# ── Gate 4: High confidence + empty input_context_refs ─────────────────────
AGENT=$(printf '%s' "$CONTENT" | grep -m1 '^source_agent:' \
  | sed 's/^source_agent:[[:space:]]*//; s/["'"'"']//g; s/[[:space:]]//g' 2>/dev/null || true)

THETA="0.85"
if [[ -n "$AGENT" ]]; then
  REP_FILE="$VAULT/_agent_state/${AGENT}/reputation.json"
  if [[ -f "$REP_FILE" ]]; then
    _theta=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    v = d.get('theta')
    print(v if isinstance(v, (int, float)) else 0.85)
except Exception:
    print(0.85)
" "$REP_FILE" 2>/dev/null)
    [[ -n "$_theta" && "$_theta" != "None" ]] && THETA="$_theta"
  fi
fi

CONF=$(printf '%s' "$CONTENT" | grep -m1 '^confidence:' \
  | grep -oE '[0-9]+\.?[0-9]*' | head -1 || true)

if [[ -n "$CONF" ]]; then
  CONF_GTE_THETA=$(python3 -c "
try:
    print('yes' if float('${CONF}') >= float('${THETA}') else 'no')
except Exception:
    print('no')
" 2>/dev/null)

  if [[ "$CONF_GTE_THETA" == "yes" ]]; then
    EMPTY_ICR=0
    printf '%s' "$CONTENT" | grep -qE '^input_context_refs:[[:space:]]*\[\][[:space:]]*$' && EMPTY_ICR=1
    if (( EMPTY_ICR == 0 )); then
      REFS_BLOCK=$(printf '%s' "$CONTENT" | \
        awk '/^input_context_refs:/{found=1; next} found && /^[a-z_]+:/{exit} found{print}' 2>/dev/null || true)
      if ! printf '%s' "$REFS_BLOCK" | grep -qE '^[[:space:]]*-[[:space:]]'; then
        if printf '%s' "$CONTENT" | grep -qE "^input_context_refs:[[:space:]]*(\"\")?('['])?[[:space:]]*$"; then
          EMPTY_ICR=1
        else
          EMPTY_ICR=1
        fi
      fi
    fi

    if (( EMPTY_ICR )); then
      deny "confidence ${CONF} >= auto-promote theta ${THETA} but input_context_refs is empty — cite the surfaces you consulted or lower confidence" \
        "§11 sbap-confidence" "high"
    fi
  fi
fi

# ── All gates passed ────────────────────────────────────────────────────────
exit 0
