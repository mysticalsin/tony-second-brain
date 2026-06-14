#!/usr/bin/env bash
# conduct-sync-check.sh — anti-drift CI check.
# The conduct core is inlined in several files; this fails (exit 1) if any inline
# copy drifts from the canonical core in infra/conduct/conduct-core.md.
# Run in brain-refresh.sh + as a pre-commit. `--list` just prints status.
set -uo pipefail

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
CORE="$VAULT/infra/conduct/conduct-core.md"

# Files that MUST carry the canonical core (inline copies + the standard's injectable).
# Adjust this list to match the files in your vault that inline the conduct core:
TARGETS=(
  "$VAULT/infra/conduct/agent-behavior-standard.md"
  "$VAULT/AGENTS.md"
  "$VAULT/GEMINI.md"
  "$VAULT/HERMES.md"
)

[[ -f "$CORE" ]] || { echo "FAIL: canonical core missing: $CORE" >&2; exit 1; }

# Extract anchors: the lines after the '## sync-anchors' heading, skipping blanks.
ANCHORS=$(awk '/^## sync-anchors/{f=1;next} f&&NF{print}' "$CORE")
[[ -n "$ANCHORS" ]] || { echo "FAIL: no sync-anchors in $CORE" >&2; exit 1; }

drift=0
echo "conduct-sync-check — canonical: infra/conduct/conduct-core.md"
echo "════════════════════════════════════════════════"
for f in "${TARGETS[@]}"; do
  name=$(basename "$f")
  if [[ ! -f "$f" ]]; then echo "  ✗ $name — FILE MISSING (skip if not yet created)"; continue; fi
  missing=()
  while IFS= read -r anchor; do
    [[ -z "$anchor" ]] && continue
    grep -Fq "$anchor" "$f" || missing+=("$anchor")
  done <<< "$ANCHORS"
  if (( ${#missing[@]} == 0 )); then
    echo "  ✓ $name — in sync"
  else
    echo "  ✗ $name — DRIFT, missing ${#missing[@]} anchor(s):"
    for m in "${missing[@]}"; do echo "        · \"$m\""; done
    drift=1
  fi
done
echo "════════════════════════════════════════════════"
if (( drift )); then
  echo "RESULT: DRIFT DETECTED — re-sync the inline copies to infra/conduct/conduct-core.md." >&2
  [[ "${1:-}" == "--list" ]] && exit 0
  exit 1
fi
echo "RESULT: all conduct cores in sync"
exit 0
