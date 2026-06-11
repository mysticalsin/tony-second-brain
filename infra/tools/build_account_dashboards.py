#!/usr/bin/env python3
"""build_account_dashboards.py — Phase 2.1
Generate one dashboard per known account at _brain_api/account/<acct>/dashboard.md.

Sources (no LLM needed — all structured):
  _agent_state/claude-code/sessions.jsonl       — sessions tagged with account
  _agent_state/<agent>/memory.json:per_account_knowledge[<acct>]
  _agent_state/<agent>/writes.jsonl             — filtered by input_context_refs
  _brain_api/bid/<bid>/status.json              — bids tied to account
  _brain_api/account/<acct>/key_contacts.json   — if populated by Graph sync (Phase 3.1)
  02_Areas/Accounts/<acct>/                     — manual notes

Called from brain-refresh.sh after build_dashboard.py. Always exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))


def load_jsonl(p: Path):
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False,
                                     prefix=".tmp-", suffix=path.suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def discover_accounts(vault: Path) -> set[str]:
    """Union of accounts from: sessions, agent memories, bid registry, Accounts/ subdirs."""
    accounts = set()

    # 1. Sessions with detected account
    for s in load_jsonl(vault / "_agent_state" / "claude-code" / "sessions.jsonl"):
        a = s.get("account")
        if a:
            accounts.add(a)

    # 2. Per-agent memory buckets
    for agent_dir in (vault / "_agent_state").iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        mem = load_json(agent_dir / "memory.json", {})
        for a in (mem.get("per_account_knowledge") or {}).keys():
            accounts.add(a)

    # 3. 02_Areas/Accounts/ subdirs
    accounts_dir = vault / "02_Areas" / "Accounts"
    if accounts_dir.exists():
        for d in accounts_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                # Normalize: lowercase, hyphenate
                import re
                slug = re.sub(r"[^a-z0-9_-]+", "-", d.name.strip().lower()).strip("-")
                if slug:
                    accounts.add(slug)

    # 4. 01_Projects/ folders prefixed by client name (Client-Opp pattern)
    projects_dir = vault / "01_Projects"
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                import re
                client = d.name.split("-")[0] if "-" in d.name else d.name
                slug = re.sub(r"[^a-z0-9_-]+", "-", client.strip().lower()).strip("-")
                if slug:
                    accounts.add(slug)

    return accounts


def gather_for_account(vault: Path, account: str) -> dict:
    """Pull everything we know about this account."""
    data = {
        "account": account,
        "sessions": [],
        "bids": [],
        "agent_knowledge": [],  # one entry per agent that knows this account
        "writes": [],            # writes that reference this account
        "key_contacts": [],
        "recent_changes": [],
    }

    # Sessions
    for s in load_jsonl(vault / "_agent_state" / "claude-code" / "sessions.jsonl"):
        if s.get("account") == account:
            data["sessions"].append({
                "ts": s.get("ts", ""),
                "model": s.get("model"),
                "cost_usd": s.get("cost_usd", 0),
                "summary": s.get("summary"),
                "topics": s.get("topics") or [],
            })
    data["sessions"].sort(key=lambda x: x.get("ts", ""), reverse=True)

    # Per-agent buckets
    for agent_dir in (vault / "_agent_state").iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        mem = load_json(agent_dir / "memory.json", {})
        bucket = (mem.get("per_account_knowledge") or {}).get(account)
        if bucket:
            data["agent_knowledge"].append({
                "agent": agent_dir.name,
                "first_seen": bucket.get("first_seen", "?"),
                "last_seen": bucket.get("last_seen", "?"),
                "sessions": bucket.get("sessions", 0),
                "learnings": bucket.get("learnings", []),
                "patterns": bucket.get("patterns", []),
                "mistakes": bucket.get("mistakes", []),
            })

        # Writes that mention this account in input_context_refs
        writes = load_jsonl(agent_dir / "writes.jsonl")
        for w in writes:
            # crude match — full impl could re-read each promoted file
            if account in (w.get("target", "") or ""):
                data["writes"].append({
                    "agent": agent_dir.name,
                    "ts": w.get("ts", ""),
                    "action": w.get("action"),
                    "target": w.get("target"),
                    "confidence": w.get("confidence"),
                })

    # Key contacts (populated by Phase 3.1 Graph sync)
    contacts_file = vault / "_brain_api" / "account" / account / "key_contacts.json"
    if contacts_file.exists():
        data["key_contacts"] = load_json(contacts_file, {}).get("contacts", [])

    # Recent vault changes mentioning the account
    accounts_subdir = vault / "02_Areas" / "Accounts" / account
    if accounts_subdir.exists():
        from os import stat
        files = sorted(accounts_subdir.rglob("*.md"), key=lambda p: -stat(p).st_mtime)[:10]
        data["recent_changes"] = [str(p.relative_to(vault)) for p in files]

    return data


def render_account_dashboard(d: dict) -> str:
    a = d["account"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"""---
type: account-dashboard
account: {a}
refreshed: {now}
tags: [account-dashboard, {a}]
---

# Account: {a}

*Auto-generated by `build/tools/build_account_dashboards.py`. Refreshed every hour.*

**Last refresh:** {now}

---
"""
    ]

    # Sessions
    parts.append(f"\n## Sessions (Claude Code) — {len(d['sessions'])} total\n")
    if d["sessions"]:
        parts.append("| When | Model | Cost | Summary |")
        parts.append("|---|---|---:|---|")
        for s in d["sessions"][:15]:
            ts = (s["ts"] or "")[:16].replace("T", " ")
            summary = (s.get("summary") or "")[:90].replace("|", "\\|")
            parts.append(f"| {ts} | {s.get('model','?')} | ${s.get('cost_usd',0):.4f} | {summary} |")
    else:
        parts.append("_no sessions tagged with this account yet_")

    # Agent knowledge
    parts.append("\n\n## Agent knowledge\n")
    if d["agent_knowledge"]:
        for ak in d["agent_knowledge"]:
            parts.append(f"\n### `{ak['agent']}` — {ak['sessions']} sessions, first seen {ak['first_seen']}, last {ak['last_seen']}\n")
            if ak["learnings"]:
                parts.append("**Learnings:**")
                for l in ak["learnings"][-5:]:
                    txt = l.get("text") if isinstance(l, dict) else l
                    parts.append(f"- {txt}")
            if ak["patterns"]:
                parts.append("\n**Patterns:**")
                for p in ak["patterns"][-5:]:
                    txt = p.get("text") if isinstance(p, dict) else p
                    parts.append(f"- {txt}")
            if ak["mistakes"]:
                parts.append("\n**Avoid:**")
                for m in ak["mistakes"][-5:]:
                    txt = m.get("text") if isinstance(m, dict) else m
                    parts.append(f"- {txt}")
    else:
        parts.append("_no agent has captured account-scoped learnings yet_")

    # Recent writes
    parts.append(f"\n\n## Recent agent writes about this account ({len(d['writes'])})\n")
    if d["writes"]:
        parts.append("| When | Agent | Action | Target |")
        parts.append("|---|---|---|---|")
        for w in sorted(d["writes"], key=lambda x: x.get("ts",""), reverse=True)[:10]:
            ts = (w["ts"] or "")[:16].replace("T", " ")
            parts.append(f"| {ts} | `{w['agent']}` | {w['action']} | {w.get('target','')} |")
    else:
        parts.append("_no agent writes reference this account yet_")

    # Key contacts (from Graph sync)
    parts.append(f"\n\n## Key contacts ({len(d['key_contacts'])})\n")
    if d["key_contacts"]:
        for c in d["key_contacts"]:
            parts.append(f"- **{c.get('name','?')}** · {c.get('role','')} · {c.get('email','')}")
    else:
        parts.append("_Microsoft Graph sync hasn't populated contacts yet (Phase 3.1). To populate: configure Graph auth via `python3 build/tools/graph_auth.py`._")

    # Recent vault changes
    parts.append(f"\n\n## Recent notes in `02_Areas/Accounts/{a}/` ({len(d['recent_changes'])})\n")
    if d["recent_changes"]:
        for f in d["recent_changes"]:
            parts.append(f"- [[{f}]]")
    else:
        parts.append(f"_no notes under `02_Areas/Accounts/{a}/` yet_")

    parts.append(f"\n\n---\n*Account dashboard for `{a}` · refreshed {now}*\n")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    args = ap.parse_args()

    vault = Path(args.vault)
    accounts = discover_accounts(vault)
    if not accounts:
        print("No accounts detected yet.")
        return 0

    out_root = vault / "_brain_api" / "account"
    written = 0
    for account in sorted(accounts):
        data = gather_for_account(vault, account)
        md = render_account_dashboard(data)
        atomic_write(out_root / account / "dashboard.md", md)
        written += 1

    print(f"Wrote {written} per-account dashboards to _brain_api/account/*/dashboard.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
