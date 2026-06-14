#!/usr/bin/env bash
# conduct-incident.sh — incident response for caught conduct violations.
#
# Usage:
#   bash infra/hooks/conduct-incident.sh <file> <reason>
#
#   <file>    Absolute path to the offending file.
#   <reason>  Short human-readable reason (e.g. "leaked-pii", "promoted-fabrication",
#             "injection-hit"). Quoted if it contains spaces.
#
# Actions (in order):
#   1. Quarantine — moves the offending file to:
#        $VAULT/_agent_state/_conduct/sbap-quarantine/incidents/<ts>-<name>
#      mkdir -p; move (never delete); non-destructive.
#
#   2. Git rollback (guarded) — if the vault is a git repo AND the file was tracked,
#      attempts `git checkout -- <file>` to restore the last committed version.
#      No-op if: not a git repo, file not tracked, or git command fails.
#
#   3. Escalation — writes a conduct-incident Markdown file to:
#        $VAULT/Important/escalations/conduct-incident-<ts>.md
#
#   4. Violation log — appends to _agent_state/_conduct/conduct-violations.jsonl
#      (source="incident", severity="high").
#
# Exit codes:
#   0  — all actions completed
#   1  — bad arguments or file does not exist.
#
# Dependencies: python3 stdlib only (no pip). bash -n clean.

set -uo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
VIOLATIONS_LOG="$VAULT/_agent_state/_conduct/conduct-violations.jsonl"
QUARANTINE_BASE="$VAULT/_agent_state/_conduct/sbap-quarantine/incidents"
ESCALATIONS_DIR="$VAULT/Important/escalations"

# ── Argument validation ───────────────────────────────────────────────────────

if [[ $# -lt 2 ]]; then
  printf 'conduct-incident.sh: ERROR — usage: conduct-incident.sh <file> <reason>\n' >&2
  exit 1
fi

OFFENDING_FILE="$1"
REASON="$2"

if [[ ! -e "$OFFENDING_FILE" ]]; then
  printf 'conduct-incident.sh: ERROR — file does not exist: %s\n' "$OFFENDING_FILE" >&2
  exit 1
fi

# ── Timestamp (UTC, compact ISO-8601, filesystem-safe) ───────────────────────

TS=$(python3 -c "
import datetime
print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
")

TS_ISO=$(python3 -c "
import datetime
print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
")

FILE_BASENAME=$(basename "$OFFENDING_FILE")

# ── Step 1: Quarantine ────────────────────────────────────────────────────────

QUARANTINE_DEST="$QUARANTINE_BASE/${TS}-${FILE_BASENAME}"

mkdir -p "$QUARANTINE_BASE"

if mv "$OFFENDING_FILE" "$QUARANTINE_DEST"; then
  printf 'conduct-incident.sh: [1/4] quarantined → %s\n' "$QUARANTINE_DEST"
else
  printf 'conduct-incident.sh: ERROR — quarantine move failed for: %s\n' "$OFFENDING_FILE" >&2
  exit 1
fi

# ── Step 2: Git rollback (guarded) ───────────────────────────────────────────

GIT_ROLLBACK_STATUS="skipped (vault is not a git repo or file was not tracked)"

if git -C "$VAULT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  REL_PATH="${OFFENDING_FILE#$VAULT/}"

  if git -C "$VAULT" ls-files --error-unmatch -- "$REL_PATH" >/dev/null 2>&1; then
    if git -C "$VAULT" checkout -- "$REL_PATH" 2>/dev/null; then
      GIT_ROLLBACK_STATUS="succeeded — restored last committed version of ${REL_PATH}"
      printf 'conduct-incident.sh: [2/4] git rollback succeeded → %s\n' "$REL_PATH"
    else
      GIT_ROLLBACK_STATUS="attempted but failed — manual recovery required for ${REL_PATH}"
      printf 'conduct-incident.sh: [2/4] WARN — git checkout failed for: %s\n' "$REL_PATH" >&2
    fi
  else
    GIT_ROLLBACK_STATUS="skipped (file was not tracked in git: ${REL_PATH})"
    printf 'conduct-incident.sh: [2/4] git rollback skipped — file not tracked\n'
  fi
else
  printf 'conduct-incident.sh: [2/4] git rollback skipped — vault is not a git repo\n'
fi

# ── Step 3: Escalation document ──────────────────────────────────────────────

ESCALATION_FILE="$ESCALATIONS_DIR/conduct-incident-${TS}.md"
mkdir -p "$ESCALATIONS_DIR"

python3 - \
  "$ESCALATION_FILE" \
  "$TS_ISO" \
  "$OFFENDING_FILE" \
  "$QUARANTINE_DEST" \
  "$REASON" \
  "$GIT_ROLLBACK_STATUS" \
  <<'ESCALATION_PY'
import sys

out_path       = sys.argv[1]
ts_iso         = sys.argv[2]
original_file  = sys.argv[3]
quarantine_dest = sys.argv[4]
reason         = sys.argv[5]
git_status     = sys.argv[6]

content = f"""---
sbap_version: "1.0"
source_agent: conduct-incident.sh
source_run_id: "conduct-incident-{ts_iso}"
generated: "{ts_iso}"
output_type: escalation_alert
target_path: "Important/escalations/conduct-incident-{ts_iso}.md"
confidence: 1.0
needs_review: true
sensitivity: internal
reasoning_summary: |
  Conduct incident triggered by: {reason}. Offending file quarantined. See body for full details.
---

# CONDUCT INCIDENT — {ts_iso}

> **ACTION REQUIRED** — review this escalation, assess blast radius, and decide next steps.

---

## What happened

A conduct violation was caught by an automated check and triggered the incident response path (`conduct-incident.sh`).

| Field | Value |
|---|---|
| Detected at | `{ts_iso}` |
| Reason / violation | `{reason}` |
| Offending file | `{original_file}` |
| Quarantined to | `{quarantine_dest}` |
| Git rollback | {git_status} |

---

## Recommended actions

1. **Review the quarantined file** — assess what was written and whether it escaped to any downstream surfaces.
2. **Assess blast radius** — check if the content was promoted to a canonical path, referenced by other notes, or surfaced in an agent response.
3. **If PII was present** — determine whether it was exposed externally; follow the DPIA / notification procedure if applicable.
4. **If fabrication was promoted** — audit any downstream knowledge surfaces that may have consumed the bad fact.
5. **Clear or reset downstream state** — revert any affected `_brain_api` or `_wiki` entries; run `conduct-sync-check.sh` to verify no drift.
6. **Close the loop** — once the incident is resolved, append a resolution note below and mark `needs_review: false`.

---

## Resolution (fill in when resolved)

- [ ] Blast radius assessed
- [ ] Downstream state cleaned
- [ ] Root cause identified
- [ ] Preventive action taken
- [ ] `needs_review` set to false

**Resolved by:** —
**Resolution date:** —
**Notes:** —

---

*Auto-generated by `infra/hooks/conduct-incident.sh`.*
"""

with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(content)

print(f"conduct-incident.sh: [3/4] escalation written → {out_path}")
ESCALATION_PY

# ── Step 4: Violations log ────────────────────────────────────────────────────

mkdir -p "$(dirname "$VIOLATIONS_LOG")"

python3 - \
  "$VIOLATIONS_LOG" \
  "$TS_ISO" \
  "$OFFENDING_FILE" \
  "$QUARANTINE_DEST" \
  "$REASON" \
  <<'LOG_PY'
import sys, json, os

log_path       = sys.argv[1]
ts_iso         = sys.argv[2]
original_file  = sys.argv[3]
quarantine_dest = sys.argv[4]
reason         = sys.argv[5]

os.makedirs(os.path.dirname(log_path), exist_ok=True)

record = {
    "ts":       ts_iso,
    "source":   "incident",
    "agent":    "conduct-incident.sh",
    "rule":     reason,
    "severity": "high",
    "detail":   f"Incident response triggered: file quarantined to {quarantine_dest}",
    "file":     original_file,
}

with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")

print(f"conduct-incident.sh: [4/4] logged to {log_path}")
LOG_PY

# ── Summary ───────────────────────────────────────────────────────────────────

printf '\nconduct-incident.sh: DONE\n'
printf '  Original file:  %s\n' "$OFFENDING_FILE"
printf '  Quarantined to: %s\n' "$QUARANTINE_DEST"
printf '  Escalation:     %s\n' "$ESCALATION_FILE"
printf '  Violations log: %s\n' "$VIOLATIONS_LOG"
printf '  Git rollback:   %s\n' "$GIT_ROLLBACK_STATUS"

exit 0
