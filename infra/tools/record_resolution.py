#!/usr/bin/env python3
"""record_resolution.py — persist a human resolution of a held Dust draft.

The confidence-calibration loop needs the HUMAN OUTCOME joined to the agent's stated
confidence. triage_dust_writes.py records {confidence, action} per write; this records
Tony's accept/reject/edit/defer decision so build_dashboard.py can compute per-agent
calibration (was the agent's confidence warranted?).

Called by the /dust-resolve command after each decision. One JSON line per resolution,
appended atomically to _agent_state/<agent>/resolutions.jsonl:

    {ts, source_file, source_run_id, confidence, agent_action, human_action, rationale}

If --confidence / --source-run-id / --agent-action are omitted, they're looked up by
source_file from the agent's writes.jsonl (the triage event stream).

Usage:
    record_resolution.py --agent email-responder \
        --source-file 2026-05-17-globex-q2-pricing-followup.md \
        --human-action reject --rationale "stale + placeholder recipient"

Exit 0 on success; non-zero only on bad input (unknown human-action, missing agent).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
HUMAN_ACTIONS = {"accept", "reject", "edit", "defer"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def lookup_from_writes(agent_dir: Path, source_file: str) -> dict:
    """Find the most recent triage event for source_file → its confidence/run_id/action."""
    wj = agent_dir / "writes.jsonl"
    found = {}
    if wj.exists():
        for line in wj.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("source_file") == source_file:
                found = ev  # keep last (most recent) match
    return found


def append_atomic(path: Path, line: str) -> None:
    """Append one line durably (read-modify-write via a temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False,
                                     prefix=".tmp-", suffix=".jsonl") as tmp:
        tmp.write(existing + line + "\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a human resolution of a held Dust draft.")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--source-file", required=True)
    ap.add_argument("--human-action", required=True, choices=sorted(HUMAN_ACTIONS))
    ap.add_argument("--confidence", type=float, default=None)
    ap.add_argument("--source-run-id", default=None)
    ap.add_argument("--agent-action", default=None, help="triage action (promoted/hold-*); looked up if omitted")
    ap.add_argument("--rationale", default="")
    ap.add_argument("--vault", default=VAULT_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault)
    agent_dir = vault / "_agent_state" / args.agent
    if not agent_dir.exists():
        print(f"ERROR: unknown agent (no {agent_dir})", file=sys.stderr)
        return 2

    ev = lookup_from_writes(agent_dir, args.source_file)
    rec = {
        "ts": utcnow(),
        "source_file": args.source_file,
        "source_run_id": args.source_run_id if args.source_run_id is not None else ev.get("source_run_id", ""),
        "confidence": args.confidence if args.confidence is not None else ev.get("confidence"),
        "agent_action": args.agent_action if args.agent_action is not None else ev.get("action", ""),
        "human_action": args.human_action,
        "rationale": args.rationale,
    }
    line = json.dumps(rec, ensure_ascii=False)
    if args.dry_run:
        print(f"DRY-RUN would append → {agent_dir / 'resolutions.jsonl'}: {line}")
        return 0
    append_atomic(agent_dir / "resolutions.jsonl", line)
    print(f"recorded {args.human_action} for {args.source_file} (agent={args.agent}, "
          f"confidence={rec['confidence']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
