#!/usr/bin/env python3
"""Reconcile every Dust agent in the registry so it can READ from and WRITE into
the vault (SBAP). Idempotent — creates only what's missing, never overwrites an
existing rich playbook or memory.

For each Dust agent it guarantees:
  • 00_Inbox/from-dust/<agent>/        + README.md (SBAP write guide)
  • _agent_state/<agent>/memory.json   (read/learning state skeleton)
  • _agent_state/<agent>/writes.jsonl  (append-only run log)
  • _agent_state/<agent>/last_ingest.json (checkpoint skeleton)
  • _agent_state/<agent>/playbook.md   (universal READ+WRITE contract, if absent)

External-CLI agents (claude-code/codex/gemini) and the 'dust' meta are skipped —
they use the session-ingest path, not 00_Inbox/from-dust/.

Also emits _agent_state/dust-onboarding-blocks.md — the per-agent text to paste
into each Dust agent's Instructions field + the connector checklist.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
REGISTRY = VAULT / "_agent_state" / "_registry.json"
EXCLUDE = {"claude-code", "codex", "gemini", "dust"}  # external-CLI / meta, not Dust writers
NOW = dt.datetime.now().astimezone().isoformat(timespec="seconds")
TODAY = NOW[:10]


def title(name: str) -> str:
    return "".join(w.capitalize() for w in name.replace("_", "-").split("-"))


def ensure_json(path: Path, payload: dict) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return True


def ensure_file(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


def playbook(agent: str, a: dict) -> str:
    name = title(agent)
    role = a.get("role") or f"Produce {a.get('output_type','output')} for Tony's bid/account work."
    otype = a.get("output_type", "other")
    sens = a.get("max_sensitivity", "internal")
    return f"""# @{name} Playbook (SBAP v1.0)

> Auto-scaffolded {TODAY}. The READ + WRITE contract below is the firm standard every
> agent follows; refine the role specifics over time. This vault is the shared agent
> memory — read from `_brain_api/`, write into `00_Inbox/from-dust/{agent}/`.

## Role
{role}

## SBAP READ protocol (mandatory before any output)
0. Read `Preferences/dont.md` (permanent rules) + the last 5 entries of `Preferences/mistakes.md`.
   A rule there overrides this playbook except Tony's direct instruction.
1. `_agent_state/{agent}/memory.json` — your own learnings/patterns from prior runs.
2. `_brain_api/account/<client>/brief.json` — account context (when client-scoped).
3. `_brain_api/bid/_open.json` or `_brain_api/bid/<bid-id>/status.json` — bid context.
4. `_brain_api/changes/since_<last_run_ts>.json` — what changed since your last run
   (your checkpoint is in `_agent_state/{agent}/last_ingest.json`).
5. `_brain_api/canonical/<type>/<key>.json` — reusable canonical blocks for your output.
Always query `_brain_api/` first; never crawl raw folders.

## SBAP WRITE protocol
File: `00_Inbox/from-dust/{agent}/<YYYY-MM-DDTHHMMSS>-<slug>.md`

Mandatory frontmatter (validated vs `99_Meta/sbap-schemas/agent_write.schema.json`):
- `sbap_version: "1.0"`
- `source_agent: "{agent}"`
- `source_run_id: <uuid>`
- `generated: <ISO timestamp>`
- `input_context_refs: [<every _brain_api/ endpoint you read>]`
- `output_type: "{otype}"`
- `target_path: <where triage should file it if promoted>`
- `confidence: <0.0-1.0>`
- `reasoning_summary: <2-3 sentences>`

Body = your deliverable in Obsidian-flavored markdown (wikilinks `[[...]]`, headers, callouts).
Triage (`build/tools/triage_dust_writes.py`) auto-promotes `confidence ≥ 0.85`; lower is
held for `/dust-resolve`. **Last-write-wins is FORBIDDEN** — conflicts become versioned
files (`<target>.dust-{agent}-<ts>.md`).

## Memory write-back (so the brain gets smarter)
After any user correction or confirmed pattern, append to
`_agent_state/{agent}/memory.json:recent_learnings` (≤25 words, dated) and log the run
in `_agent_state/{agent}/writes.jsonl` (one JSON line: run_id, generated, output_type,
target_path, confidence).

## Sensitivity
Max sensitivity: **{sens}**. Never reproduce client-confidential content verbatim outside
the vault. Run a confidentiality check on any external-facing output before it leaves the vault.
"""


def inbox_readme(agent: str, a: dict) -> str:
    return f"""# 00_Inbox/from-dust/{agent}/

Drop zone for **@{title(agent)}** ({a.get('output_type','output')}) SBAP writes.

Every `.md` here MUST carry valid SBAP frontmatter (see
`_agent_state/{agent}/playbook.md` → WRITE protocol). The PostToolUse hook
`99_Meta/validate-md-write.sh` logs violations; triage promotes
`confidence ≥ 0.85` and holds the rest for `/dust-resolve`.
"""


def main() -> int:
    reg = json.loads(REGISTRY.read_text())
    agents = reg["agents"]
    created = {"inbox": [], "readme": [], "memory": [], "writes": [], "checkpoint": [], "playbook": []}
    dust_agents = []

    for a in agents:
        name = a.get("agent_name")
        if not name or name in EXCLUDE:
            continue
        dust_agents.append((name, a))

        inbox = VAULT / "00_Inbox" / "from-dust" / name
        if not inbox.exists():
            inbox.mkdir(parents=True, exist_ok=True)
            created["inbox"].append(name)
        if ensure_file(inbox / "README.md", inbox_readme(name, a)):
            created["readme"].append(name)

        state = VAULT / "_agent_state" / name
        if ensure_json(state / "memory.json", {
            "agent": name, "memory_version": 1, "last_updated": NOW,
            "global_patterns": [], "per_account_knowledge": {},
            "self_observations": [], "recent_learnings": [],
        }):
            created["memory"].append(name)
        if ensure_file(state / "writes.jsonl", ""):
            created["writes"].append(name)
        if ensure_json(state / "last_ingest.json", {
            "agent": name, "last_run_ts": None, "last_write_ts": None, "runs": 0,
        }):
            created["checkpoint"].append(name)
        if ensure_file(state / "playbook.md", playbook(name, a)):
            created["playbook"].append(name)

    # Dust onboarding paste-blocks doc
    blocks = [
        "---", "type: dust-onboarding", f"generated: {NOW}", "tags: [sbap, dust, onboarding]", "---", "",
        "# Dust Onboarding — paste blocks",
        "",
        "For EACH Dust agent below: open it in Dust, paste the block into its **Instructions** "
        "field, and set **Connectors → OneDrive (read + write)** so it can read `_brain_api/` and "
        "write to `00_Inbox/from-dust/<agent>/`. (Connector config is the only step Claude can't "
        "do for you — everything in the vault is already scaffolded.)",
        "",
        "> Vault root: `" + str(VAULT) + "`",
        "",
    ]
    for name, a in sorted(dust_agents):
        blocks += [
            f"## @{title(name)}  ·  `{a.get('output_type','')}`  ·  status: {a.get('status','')}",
            "```",
            f"You are @{title(name)}. Your operating manual is at:",
            f"{VAULT}/_agent_state/{name}/playbook.md",
            "",
            "Before ANY work: read that playbook and follow it exactly. Read the vault's "
            "_brain_api/ endpoints (not raw folders). Read Preferences/dont.md and the last 5 "
            "entries of Preferences/mistakes.md first. Write your output to "
            f"00_Inbox/from-dust/{name}/ with the mandatory SBAP frontmatter from the playbook. "
            "If the playbook is unavailable, refuse to act and report an error.",
            "```",
            "",
        ]
    ensure_file_overwrite = VAULT / "_agent_state" / "dust-onboarding-blocks.md"
    tmp = ensure_file_overwrite.with_suffix(".md.tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    os.replace(tmp, ensure_file_overwrite)

    print(f"Dust agents reconciled: {len(dust_agents)}")
    for k, v in created.items():
        if v:
            print(f"  created {k}: {len(v)} → {', '.join(sorted(v))}")
    if not any(created.values()):
        print("  (all scaffolding already present — nothing to create)")
    print(f"Onboarding blocks → {ensure_file_overwrite}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
