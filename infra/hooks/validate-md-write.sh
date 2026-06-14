#!/usr/bin/env bash
# validate-md-write.sh — PostToolUse:Write|Edit hook.
# Validates SBAP frontmatter on any .md write inside 00_Inbox/from-dust/.
# Other vault writes pass through silently.
#
# Hook input on stdin (JSON):
#   { "tool_input": { "file_path": "..." }, "tool_response": {...} }
#
# Exit 0 always (informational, never blocks).
# Errors land in $VAULT/99_Meta/md-validation-errors.log (or a path you configure).

set -uo pipefail

[[ "${VAULT_BRAIN_QUIET:-0}" == "1" ]] && exit 0

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
ERR_LOG="$VAULT/99_Meta/md-validation-errors.log"

PAYLOAD=$(cat 2>/dev/null || echo '{}')
FILE=$(echo "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('file_path', '') or d.get('tool_response', {}).get('filePath', ''))
except Exception:
    print('')
" 2>/dev/null)

# Only check .md files inside the SBAP write inbox
case "$FILE" in
  "$VAULT/00_Inbox/from-dust/"*.md) ;;
  *) exit 0 ;;
esac

[[ ! -f "$FILE" ]] && exit 0

HEAD=$(head -50 "$FILE" 2>/dev/null)

if ! echo "$HEAD" | head -1 | grep -q '^---$'; then
  mkdir -p "$(dirname "$ERR_LOG")"
  printf '%s\n' "$(date -Iseconds) MISSING_FRONTMATTER  $FILE" >> "$ERR_LOG"
  exit 0
fi

missing=()
for field in sbap_version source_agent source_run_id generated input_context_refs output_type target_path confidence; do
  if ! echo "$HEAD" | grep -qE "^${field}:"; then
    missing+=("$field")
  fi
done

if (( ${#missing[@]} > 0 )); then
  mkdir -p "$(dirname "$ERR_LOG")"
  printf '%s\n' "$(date -Iseconds) INVALID_FRONTMATTER  $FILE  missing=[$(IFS=,; echo "${missing[*]}")]" >> "$ERR_LOG"
fi

# Secret scan
if grep -qE '(sk-ant-[a-zA-Z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[a-zA-Z0-9]{36})' "$FILE" 2>/dev/null; then
  mkdir -p "$(dirname "$ERR_LOG")"
  printf '%s\n' "$(date -Iseconds) SECRET_DETECTED  $FILE" >> "$ERR_LOG"
fi

# ── Conduct teeth (§1/§11) → conduct-violations.jsonl ──
CLOG="$VAULT/_agent_state/_conduct/conduct-violations.jsonl"
mkdir -p "$(dirname "$CLOG")"
AGENT=$(echo "$HEAD" | grep -m1 '^source_agent:' | sed 's/^source_agent:[[:space:]]*//; s/["'"'"']//g; s/[[:space:]]//g' 2>/dev/null || echo "unknown")
log_conduct() { # $1=rule $2=severity $3=detail
  printf '{"ts":"%s","source":"md-write-hook","agent":"%s","rule":"%s","severity":"%s","detail":"%s","file":"%s"}\n' \
    "$(date -Iseconds)" "${AGENT:-unknown}" "$1" "$2" "$3" "$FILE" >> "$CLOG" 2>/dev/null || true
}

# Unresolved placeholder in target_path
echo "$HEAD" | grep -qE '^target_path:.*(\[[A-Z_]+\]|<[a-z_]+>|TYPE_REQUIRED)' && \
  log_conduct "11-SBAP-provenance" "high" "target_path has an unresolved placeholder"

# Empty source_run_id
echo "$HEAD" | grep -qE '^source_run_id:[[:space:]]*("")?[[:space:]]*$' && \
  log_conduct "11-SBAP-provenance" "high" "empty source_run_id"

# Empty input_context_refs
EMPTY_ICR=0
echo "$HEAD" | grep -qE '^input_context_refs:[[:space:]]*\[\][[:space:]]*$' && EMPTY_ICR=1
if echo "$HEAD" | grep -qE '^input_context_refs:[[:space:]]*$' && ! echo "$HEAD" | grep -A1 '^input_context_refs:' | grep -qE '^[[:space:]]*-[[:space:]]'; then EMPTY_ICR=1; fi
(( EMPTY_ICR )) && log_conduct "1-cite-the-surface" "med" "input_context_refs empty — output claims no sources consulted"

# High confidence + empty refs — possible gate gaming
CONF=$(echo "$HEAD" | grep -m1 '^confidence:' | grep -oE '[0-9]+\.?[0-9]*' | head -1 || echo "")
REP_FILE="$VAULT/_agent_state/${AGENT:-_none}/reputation.json"
THETA="0.85"
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
if [[ -n "$CONF" ]]; then
  CONF_GTE=$(python3 -c "
try:
    print('yes' if float('${CONF}') >= float('${THETA}') else 'no')
except Exception:
    print('no')
" 2>/dev/null)
  if [[ "$CONF_GTE" == "yes" ]] && (( EMPTY_ICR )); then
    log_conduct "11-honest-confidence" "high" "confidence $CONF >= auto-promote theta $THETA with empty input_context_refs — possible gaming of the gate"
  fi
fi

exit 0
