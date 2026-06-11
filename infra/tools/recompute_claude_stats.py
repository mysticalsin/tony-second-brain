#!/usr/bin/env python3
"""recompute_claude_stats.py — rebuild _agent_state/claude-code/stats.json from the
REAL Claude Code transcripts, with accurate per-message-model pricing.

Why: the incremental capture hook only tallied sessions it happened to catch, and the
old exact-string pricing under-priced unlisted models (Opus 4.8 fell back to Sonnet).
This walks every ~/.claude/projects transcript (top-level sessions AND subagent
transcripts — both incur real API cost), re-derives true usage + cost (each assistant
turn priced by ITS OWN model via capture_session.price_for), and writes a corrected
stats.json in the SAME short-key schema the dashboard + live capture use.

Sessions are counted from TOP-LEVEL transcripts only (subagent agent-*.jsonl files add
their tokens/cost but are not separate "sessions").

Usage:
  python3 recompute_claude_stats.py [--dry-run]
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import capture_session as cs  # reuse parse_transcript + price_for (same dir)

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
STATS = VAULT / "_agent_state" / "claude-code" / "stats.json"
PROJECTS = Path.home() / ".claude" / "projects"

# transcript-usage (long) → stats.json schema (short)
SRC = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
}


def local_day(ts: str) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return None


def blank() -> dict:
    d = {k: 0 for k in SRC}
    d["sessions"] = 0
    d["cost_usd"] = 0.0
    return d


def add(bucket: dict, usage_long: dict, cost: float, count_session: bool) -> None:
    for short, long in SRC.items():
        bucket[short] += int(usage_long.get(long, 0) or 0)
    if count_session:
        bucket["sessions"] += 1
    bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)


def is_subagent(path: Path) -> bool:
    return "subagents" in path.parts or path.name.startswith("agent-")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    all_time, by_day, by_model = blank(), defaultdict(blank), defaultdict(blank)
    transcripts = sorted(PROJECTS.glob("**/*.jsonl"))
    sessions = subagents = skipped = 0

    for path in transcripts:
        facts = cs.parse_transcript(path)
        if not facts.get("cost_usd") and not any(facts["usage"].values()):
            skipped += 1
            continue
        sub = is_subagent(path)
        cost = facts.get("cost_usd") or cs.compute_cost(facts["usage"], facts["model"])
        add(all_time, facts["usage"], cost, count_session=not sub)
        day = local_day(facts["first_ts"]) or "unknown"
        add(by_day[day], facts["usage"], cost, count_session=not sub)
        # accurate per-model attribution straight from the transcript breakdown
        for mdl, b in (facts.get("by_model") or {}).items():
            tgt = by_model[mdl]
            for short, long in SRC.items():
                tgt[short] += int(b.get(long, 0) or 0)
            tgt["cost_usd"] = round(tgt["cost_usd"] + b.get("cost_usd", 0.0), 6)
            if not sub:
                tgt["sessions"] += 1
        sessions += 0 if sub else 1
        subagents += 1 if sub else 0

    all_time["capture_overhead_usd"] = 0.0
    out = {"all_time": all_time, "by_day": dict(by_day), "by_model": dict(by_model)}

    print(f"transcripts: {len(transcripts)} | sessions: {sessions} | subagent files: {subagents} | empty: {skipped}")
    a = all_time
    print(f"all_time: sessions={a['sessions']} in={a['input_tokens']:,} out={a['output_tokens']:,} "
          f"cache_read={a['cache_read_tokens']:,} cost=${a['cost_usd']:.2f}")
    print("by_model:", {m: f"${b['cost_usd']:.2f}" for m, b in sorted(by_model.items(), key=lambda kv: -kv[1]['cost_usd'])})

    if args.dry_run:
        print("(dry-run — not written)")
        return 0
    if STATS.exists():
        STATS.with_suffix(".json.bak").write_text(STATS.read_text())
        print("backed up old stats → stats.json.bak")
    STATS.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {STATS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
