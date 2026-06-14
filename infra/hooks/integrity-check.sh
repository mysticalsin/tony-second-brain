#!/usr/bin/env bash
# integrity-check.sh  —  data-layer integrity guard.
#
# Usage:
#   bash infra/hooks/integrity-check.sh            # Compare vs baseline; report mutations + conflicts
#   bash infra/hooks/integrity-check.sh --update   # Re-baseline after owner confirms intended changes
#
# Canonical paths monitored (relative to VAULT):
#   _brain_api/**/*.json
#   _wiki/**/*.md
#   infra/conduct/agent-behavior-standard.md
#   CLAUDE.md  AGENTS.md  GEMINI.md  HERMES.md
#   infra/conduct/conduct-core.md
#
# Baseline:  _agent_state/_conduct/integrity-baseline.json
#
# On hash-changed file  → print + append to _agent_state/_conduct/conduct-violations.jsonl (source="integrity")
# On sync conflict      → print (no violation entry; these are filesystem artefacts)
# Exit codes:
#   0  = clean (or --update completed)
#   1  = at least one mutation or conflict found
#
# bash -n clean; python3 -m py_compile clean; stdlib only; idempotent.
# Non-destructive: never deletes, moves, or modifies canonical files.

set -uo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
BASELINE_FILE="$VAULT/_agent_state/_conduct/integrity-baseline.json"
VIOLATIONS_LOG="$VAULT/_agent_state/_conduct/conduct-violations.jsonl"

# ── Mode ──────────────────────────────────────────────────────────────────────

UPDATE_MODE=0
[[ "${1:-}" == "--update" ]] && UPDATE_MODE=1

# ── Ensure required dirs exist ────────────────────────────────────────────────

mkdir -p "$(dirname "$BASELINE_FILE")"
mkdir -p "$(dirname "$VIOLATIONS_LOG")"

# ── All logic in Python3 (sha256, JSON, glob — all stdlib) ───────────────────

python3 - "$VAULT" "$BASELINE_FILE" "$VIOLATIONS_LOG" "$UPDATE_MODE" <<'PYEOF'
import sys
import os
import json
import hashlib
import glob
import datetime
import re

vault          = sys.argv[1]
baseline_file  = sys.argv[2]
violations_log = sys.argv[3]
update_mode    = sys.argv[4] == "1"

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

# ── Canonical path specs (relative glob patterns) ─────────────────────────────

CANONICAL_GLOBS = [
    "_brain_api/**/*.json",
    "_wiki/**/*.md",
    "infra/conduct/agent-behavior-standard.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "HERMES.md",
    "infra/conduct/conduct-core.md",
]

# ── Collect files matching canonical globs ────────────────────────────────────

def collect_canonical_files(vault_root: str) -> "dict[str, str]":
    result = {}
    for pattern in CANONICAL_GLOBS:
        full_pattern = os.path.join(vault_root, pattern)
        for path in glob.glob(full_pattern, recursive=True):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, vault_root)
            try:
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
                result[rel] = h.hexdigest()
            except OSError as exc:
                print(f"[WARN] Cannot read {rel}: {exc}", file=sys.stderr)
    return result


# ── Detect sync conflict files in the whole vault ─────────────────────────────

CONFLICT_PATTERNS = [
    re.compile(r"\.sync-conflict-", re.IGNORECASE),
    re.compile(r"\(conflicted copy", re.IGNORECASE),
    re.compile(r"\(Copie en conflit", re.IGNORECASE),
    re.compile(r"\.conflicted\b", re.IGNORECASE),
]

def find_conflict_files(vault_root: str) -> "list[str]":
    found = []
    skip_dirs = {".git", ".obsidian", ".trash", ".playwright-mcp", ".pytest_cache"}
    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith("._")]
        for fname in filenames:
            if any(pat.search(fname) for pat in CONFLICT_PATTERNS):
                full = os.path.join(dirpath, fname)
                found.append(os.path.relpath(full, vault_root))
    return sorted(found)


# ── Load existing baseline ─────────────────────────────────────────────────────

def load_baseline(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] Could not load baseline: {exc}", file=sys.stderr)
        return None


# ── Write baseline ─────────────────────────────────────────────────────────────

def write_baseline(path: str, vault_root: str, file_hashes: "dict[str, str]") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generated": NOW,
        "vault": vault_root,
        "files": dict(sorted(file_hashes.items())),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ── Append violation record ────────────────────────────────────────────────────

def append_violation(log_path: str, rel_path: str, detail: str, severity: str = "high") -> None:
    record = {
        "ts":       NOW,
        "source":   "integrity",
        "agent":    "integrity-check",
        "rule":     "§5.3 canonical-file-mutation",
        "severity": severity,
        "detail":   detail,
        "file":     os.path.join(vault, rel_path),
    }
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC
# ─────────────────────────────────────────────────────────────────────────────

current_hashes = collect_canonical_files(vault)

if update_mode:
    write_baseline(baseline_file, vault, current_hashes)
    file_count = len(current_hashes)
    print(f"integrity-check --update: baseline updated ({file_count} files) → {baseline_file}")
    sys.exit(0)

baseline = load_baseline(baseline_file)
if baseline is None:
    write_baseline(baseline_file, vault, current_hashes)
    file_count = len(current_hashes)
    print(f"integrity-check: FIRST RUN — baseline created ({file_count} files).")
    print(f"  Baseline: {baseline_file}")
    print(f"  Re-run without --update to compare against this baseline.")
    sys.exit(0)

old_hashes = baseline.get("files", {})
baseline_ts = baseline.get("generated", "unknown")

mutations = []
added_files = []
removed_files = []

for rel, old_hash in sorted(old_hashes.items()):
    new_hash = current_hashes.get(rel)
    if new_hash is None:
        removed_files.append(rel)
    elif new_hash != old_hash:
        mutations.append(rel)

for rel in sorted(current_hashes):
    if rel not in old_hashes:
        added_files.append(rel)

conflict_files = find_conflict_files(vault)

sep = "─" * 60
problems = bool(mutations or removed_files or conflict_files)

print(f"\n{sep}")
print("  INTEGRITY CHECK REPORT")
print(f"  Vault:     {vault}")
print(f"  Baseline:  {baseline_ts}")
print(f"  Checked:   {NOW}")
print(f"  Canonical: {len(old_hashes)} files in baseline, {len(current_hashes)} now")
print(f"{sep}\n")

if mutations:
    print(f"[MUTATION] {len(mutations)} canonical file(s) changed since baseline:")
    for rel in mutations:
        print(f"  - {rel}")
        detail = f"Canonical file hash changed since baseline ({baseline_ts}): {rel}"
        append_violation(violations_log, rel, detail, severity="high")
        print(f"    → violation logged to conduct-violations.jsonl")
    print()

if removed_files:
    print(f"[REMOVED] {len(removed_files)} canonical file(s) no longer found:")
    for rel in removed_files:
        print(f"  - {rel}")
        detail = f"Canonical file removed/missing since baseline ({baseline_ts}): {rel}"
        append_violation(violations_log, rel, detail, severity="high")
        print(f"    → violation logged to conduct-violations.jsonl")
    print()

if added_files:
    print(f"[INFO] {len(added_files)} new canonical file(s) not in baseline (informational):")
    for rel in added_files:
        print(f"  + {rel}")
    print(f"  Run --update to include these in the baseline.\n")

if conflict_files:
    print(f"[CONFLICT] {len(conflict_files)} sync conflict file(s) found in vault:")
    for rel in conflict_files:
        print(f"  ! {rel}")
    print(f"  Review and delete these. They may indicate a sync collision with a canonical file.\n")

if not problems and not added_files:
    print(f"integrity-check: OK — {len(old_hashes)} canonical files unchanged, no conflicts.\n")
elif not problems:
    print(f"integrity-check: OK (with new files noted above) — no mutations, no conflicts.\n")
else:
    parts = []
    if mutations:
        parts.append(f"{len(mutations)} mutation(s)")
    if removed_files:
        parts.append(f"{len(removed_files)} removal(s)")
    if conflict_files:
        parts.append(f"{len(conflict_files)} conflict file(s)")
    print(f"integrity-check: PROBLEMS FOUND — {', '.join(parts)}.")
    print(f"  If changes were intentional, run:  bash infra/hooks/integrity-check.sh --update\n")
    sys.exit(1)

sys.exit(0)
PYEOF
