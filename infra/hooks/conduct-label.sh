#!/usr/bin/env bash
# conduct-label.sh — ground-truth outcome labeling for the SBAP reputation gate
#
# Usage:
#   conduct-label.sh <agent> good|bad [note]
#
# What it does:
#   1. Ensures _agent_state/<agent>/ exists (creates a minimal reputation.json scaffold if new)
#   2. Maps good → --record-accept, bad → --record-trip  (reputation.py's interface)
#   3. Appends a labeled-outcome entry to _agent_state/<agent>/outcome-labels.jsonl
#   4. Invokes reputation.py so theta recomputes immediately
#   5. Prints the new theta + n_labeled
#
# Rules:
#   - bash -n clean; stdlib python3 only; idempotent; non-destructive
#   - VAULT_ROOT env var required (see infra/hooks/brain-refresh.sh for convention)
#   - Works from any cwd

set -euo pipefail

# ── paths ──────────────────────────────────────────────────────────────────────
VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
AGENT_STATE="$VAULT/_agent_state"
REP_PY="$VAULT/build/tools/reputation.py"

# ── args ───────────────────────────────────────────────────────────────────────
usage() {
    echo "Usage: $(basename "$0") <agent> good|bad [note]" >&2
    echo "  agent  : name of the Dust/SBAP agent (folder name under _agent_state/)" >&2
    echo "  good   : agent output was accepted / correct → --record-accept" >&2
    echo "  bad    : agent output was rejected / wrong   → --record-trip" >&2
    echo "  note   : optional free-text reason (quoted)" >&2
    exit 1
}

[[ $# -lt 2 ]] && usage

AGENT="$1"
VERDICT="$2"
NOTE="${3:-}"
TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# ── validate verdict ───────────────────────────────────────────────────────────
case "$VERDICT" in
    good|bad) ;;
    *) echo "ERROR: verdict must be 'good' or 'bad', got '$VERDICT'" >&2; exit 1 ;;
esac

# ── validate reputation.py ─────────────────────────────────────────────────────
if [[ ! -f "$REP_PY" ]]; then
    echo "ERROR: reputation.py not found at $REP_PY" >&2
    echo "  (Is the build symlink healthy? ls -la \"$VAULT/build\")" >&2
    exit 2
fi

# ── ensure agent state dir exists ─────────────────────────────────────────────
AGENT_DIR="$AGENT_STATE/$AGENT"
REP_JSON="$AGENT_DIR/reputation.json"

if [[ ! -d "$AGENT_DIR" ]]; then
    echo "INFO: $AGENT_DIR does not exist — creating scaffold"
    mkdir -p "$AGENT_DIR"
fi

if [[ ! -f "$REP_JSON" ]]; then
    echo "INFO: seeding minimal reputation.json for $AGENT"
    python3 - <<PYEOF
import json, pathlib, datetime
path = pathlib.Path("$REP_JSON")
path.write_text(json.dumps({
    "alpha": 2.0,
    "beta": 2.0,
    "R": 0.5,
    "n_labeled": 0,
    "theta": 0.85,
    "pinned": True,
    "signals": {
        "promoted": 0, "quarantined": 0,
        "conf_hold": 0, "neutral_hold": 0, "total": 0
    },
    "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "recovery": {
        "clean_streak": 0,
        "theta_override": None,
        "last_streak_reset": None,
        "alpha_bonus": 0.0
    }
}, indent=2), encoding="utf-8")
PYEOF
fi

# ── append to outcome-labels.jsonl (audit trail) ──────────────────────────────
LABELS_FILE="$AGENT_DIR/outcome-labels.jsonl"
python3 - <<PYEOF
import json, pathlib
entry = {
    "ts": "$TS",
    "agent": "$AGENT",
    "verdict": "$VERDICT",
    "note": "$NOTE"
}
with open("$LABELS_FILE", "a", encoding="utf-8") as f:
    f.write(json.dumps(entry) + "\n")
PYEOF

LABELS_BEFORE=$(wc -l < "$LABELS_FILE" 2>/dev/null || echo 0)

# ── call reputation.py ─────────────────────────────────────────────────────────
REP_FLAG="--record-accept"
[[ "$VERDICT" == "bad" ]] && REP_FLAG="--record-trip"

echo ""
echo "==> conduct-label: $AGENT | $VERDICT | $TS"
[[ -n "$NOTE" ]] && echo "    note: $NOTE"
echo "==> invoking: python3 build/tools/reputation.py $REP_FLAG $AGENT"
echo ""

VAULT_ROOT="$VAULT" python3 "$REP_PY" "$REP_FLAG" "$AGENT"

# ── print summary ──────────────────────────────────────────────────────────────
python3 - <<PYEOF
import json, pathlib, sys
rep_path = pathlib.Path("$REP_JSON")
labels_path = pathlib.Path("$LABELS_FILE")
try:
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"WARNING: could not read reputation.json: {e}", file=sys.stderr)
    sys.exit(0)

n_labels = sum(1 for _ in labels_path.open(encoding="utf-8") if _.strip()) if labels_path.exists() else 0
streak = rep.get("recovery", {}).get("clean_streak", 0)
print("")
print(f"  agent        : $AGENT")
print(f"  verdict      : $VERDICT")
print(f"  theta        : {rep['theta']:.4f}  {'(PINNED — < 10 labels)' if rep.get('pinned') else '(floating)'}")
print(f"  R            : {rep['R']:.4f}")
print(f"  clean_streak : {streak} / 5")
print(f"  n_outcome_labels (this file): {n_labels}")
print(f"  n_labeled (writes.jsonl-based): {rep['n_labeled']}")
print(f"  updated      : {rep['updated']}")
print("")
print("  reputation.json → $REP_JSON")
print("  outcome-labels  → $LABELS_FILE")
PYEOF
