"""Maintain _agent_state/. Pre-flight check + post-run update.

SBAP v1.0 — per-agent persistent memory lifecycle.

Module API:
    get_agent_context(agent_name) -> dict
        Called before every agent invocation. Returns:
            - playbook (system prompt)
            - memory (learned patterns + per-account knowledge)
            - last_run timestamp (for changefeed delta)
            - scope (what they can read/write)

    update_agent_state(agent_name, run_summary)
        Called after every invocation. Updates:
            - memory.json (if agent reported learning)
            - last_run.json
            - stats.json (increment counters)
            - writes.jsonl (append write records)

    initialize_agent(agent_name, playbook_md, scope)
        Register new agent.

    prune_agent_state(agent_name, days=30)
        Weekly: drop write records older than N days from writes.jsonl.

CLI:
    python3 agent_state_manager.py <agent> get-context
    python3 agent_state_manager.py <agent> update --summary <json>
    python3 agent_state_manager.py <agent> prune --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
AGENT_STATE = VAULT / "_agent_state"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry() -> dict:
    p = AGENT_STATE / "_registry.json"
    if not p.exists():
        return {"agents": []}
    return json.loads(p.read_text())


def find_agent(name: str) -> dict | None:
    reg = load_registry()
    for a in reg.get("agents", []):
        if a["agent_name"] == name:
            return a
    return None


def get_agent_context(agent_name: str) -> dict:
    agent = find_agent(agent_name)
    if not agent:
        raise SystemExit(f"Unknown agent: {agent_name}. Register in _agent_state/_registry.json first.")
    d = AGENT_STATE / agent_name
    playbook = (d / "playbook.md").read_text() if (d / "playbook.md").exists() else ""
    memory = json.loads((d / "memory.json").read_text()) if (d / "memory.json").exists() else {}
    last_run_path = d / "last_run.json"
    last_run = json.loads(last_run_path.read_text()) if last_run_path.exists() else {}
    return {
        "agent_name": agent_name,
        "playbook": playbook,
        "memory": memory,
        "last_run": last_run,
        "scope": agent.get("scope", {}),
        "max_sensitivity": agent.get("max_sensitivity", "internal"),
        "now": utcnow(),
    }


def update_agent_state(agent_name: str, run_summary: dict) -> None:
    d = AGENT_STATE / agent_name
    d.mkdir(parents=True, exist_ok=True)

    # last_run.json
    (d / "last_run.json").write_text(json.dumps(run_summary, indent=2))

    # stats.json — increment counters
    stats_path = d / "stats.json"
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {
        "agent": agent_name, "total_runs": 0, "successful_runs": 0,
        "avg_confidence": 0.0, "last_updated": utcnow(),
    }
    stats["total_runs"] += 1
    if run_summary.get("status") == "success":
        stats["successful_runs"] += 1
    if "confidence" in run_summary:
        prev = stats.get("avg_confidence", 0.0)
        n = stats["successful_runs"] or 1
        stats["avg_confidence"] = (prev * (n - 1) + run_summary["confidence"]) / n
    stats["last_updated"] = utcnow()
    stats_path.write_text(json.dumps(stats, indent=2))

    # writes.jsonl — append
    if run_summary.get("write_record"):
        writes = d / "writes.jsonl"
        with writes.open("a") as f:
            f.write(json.dumps(run_summary["write_record"]) + "\n")

    # memory.json — merge if agent reported new learning
    if run_summary.get("memory_delta"):
        mem_path = d / "memory.json"
        mem = json.loads(mem_path.read_text()) if mem_path.exists() else {}
        delta = run_summary["memory_delta"]
        for key in ("global_patterns", "self_observations"):
            mem.setdefault(key, []).extend(delta.get(key, []))
        for acct, knowledge in delta.get("per_account_knowledge", {}).items():
            mem.setdefault("per_account_knowledge", {})[acct] = knowledge
        mem["last_updated"] = utcnow()
        mem_path.write_text(json.dumps(mem, indent=2))


def prune_agent_state(agent_name: str, days: int = 30) -> int:
    """Drop writes.jsonl records older than `days`."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    writes = AGENT_STATE / agent_name / "writes.jsonl"
    if not writes.exists():
        return 0
    kept, dropped = [], 0
    for line in writes.read_text().splitlines():
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec.get("generated", "1970-01-01T00:00:00+00:00"))
            if ts >= cutoff:
                kept.append(line)
            else:
                dropped += 1
        except (json.JSONDecodeError, ValueError):
            kept.append(line)
    writes.write_text("\n".join(kept) + ("\n" if kept else ""))
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", help="Agent name (from _registry.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get-context")
    p_update = sub.add_parser("update")
    p_update.add_argument("--summary", required=True, help="JSON run summary")
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if args.cmd == "get-context":
        print(json.dumps(get_agent_context(args.agent), indent=2))
    elif args.cmd == "update":
        update_agent_state(args.agent, json.loads(args.summary))
        print(f"Updated state for {args.agent}")
    elif args.cmd == "prune":
        n = prune_agent_state(args.agent, args.days)
        print(f"Pruned {n} records older than {args.days}d for {args.agent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
