#!/usr/bin/env python3
"""backfill_per_account_memory.py — retro-populate per_account_knowledge from past sessions.

The per-account memory split landed in capture_session.py *after* the first
sessions were captured. Existing entries in sessions.jsonl don't have an
`account` field, and memory.json's `per_account_knowledge: {}` is empty.

This script walks sessions.jsonl, runs detect_account() on each record's cwd,
and rebuilds per_account_knowledge from the existing learnings/patterns. It is
safe to run repeatedly — the merge logic dedupes.

Usage:
    python3 build/tools/backfill_per_account_memory.py
    python3 build/tools/backfill_per_account_memory.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import detect_account from capture_session — single source of truth
sys.path.insert(0, str(Path(__file__).parent))
try:
    from capture_session import detect_account
except ImportError:
    print("error: capture_session.py not importable — keep both files in build/tools/", file=sys.stderr)
    sys.exit(1)

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", default=VAULT_DEFAULT)
    args = ap.parse_args()

    vault = Path(args.vault)
    sessions_jsonl = vault / "_agent_state" / "claude-code" / "sessions.jsonl"
    memory_path = vault / "_agent_state" / "claude-code" / "memory.json"

    if not sessions_jsonl.exists():
        print(f"no sessions.jsonl at {sessions_jsonl}")
        return 0

    # Load all sessions
    sessions = []
    with sessions_jsonl.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sessions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Group by detected account
    by_account: dict[str, dict] = {}
    no_account = 0
    for s in sessions:
        account = s.get("account") or detect_account(s.get("cwd", ""), s.get("topics") or [])
        if not account:
            no_account += 1
            continue
        bucket = by_account.setdefault(account, {
            "first_seen": s.get("ts", "")[:10] or "1970-01-01",
            "last_seen": s.get("ts", "")[:10] or "1970-01-01",
            "sessions": 0,
            "learnings": [],
            "patterns": [],
            "mistakes": [],
        })
        bucket["sessions"] += 1
        date = s.get("ts", "")[:10]
        if date and date < bucket["first_seen"]:
            bucket["first_seen"] = date
        if date and date > bucket["last_seen"]:
            bucket["last_seen"] = date
        for l in s.get("learnings") or []:
            if l and l not in [x.get("text") if isinstance(x, dict) else x for x in bucket["learnings"]]:
                bucket["learnings"].append({"date": date, "text": l})
        for p in s.get("patterns") or []:
            if p and p not in [x.get("text") if isinstance(x, dict) else x for x in bucket["patterns"]]:
                bucket["patterns"].append({"date": date, "text": p})
        for m in s.get("mistakes_to_avoid") or []:
            if m and m not in [x.get("text") if isinstance(x, dict) else x for x in bucket["mistakes"]]:
                bucket["mistakes"].append({"date": date, "text": m})

    # Cap per-account lists at 100 each
    for b in by_account.values():
        for key in ("learnings", "patterns", "mistakes"):
            b[key] = b[key][-100:]

    print(f"Scanned {len(sessions)} sessions:")
    print(f"  {no_account} had no detectable account")
    print(f"  {len(by_account)} unique accounts found: {sorted(by_account.keys())}")
    for acct, b in sorted(by_account.items()):
        print(f"    {acct:30} {b['sessions']:>3} sessions  "
              f"{len(b['learnings'])} learnings  {len(b['patterns'])} patterns")

    if args.dry_run:
        print("\nDRY RUN — no changes written.")
        return 0

    # Merge into memory.json
    try:
        mem = json.loads(memory_path.read_text())
    except (OSError, json.JSONDecodeError):
        mem = {"agent": "claude-code", "memory_version": 1, "last_updated": None,
               "global_patterns": [], "per_account_knowledge": {}, "self_observations": [],
               "recent_learnings": []}

    existing = mem.setdefault("per_account_knowledge", {})
    for acct, b in by_account.items():
        existing[acct] = b  # overwrite — we just rebuilt from canonical source

    mem["last_updated"] = datetime.now(timezone.utc).isoformat()
    memory_path.write_text(json.dumps(mem, indent=2) + "\n")
    print(f"\nWrote {len(by_account)} account buckets to {memory_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
