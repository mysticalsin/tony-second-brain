#!/usr/bin/env python3
"""contradiction_detector.py — Cross-agent belief contradiction detector.

Scans every _agent_state/<agent>/memory.json for belief tuples
(entity, belief_type, value) extracted deterministically via regex gazetteer,
then pairwise-compares across agents to surface contradictions.

Extraction pipeline (NO LLM in core path):
  1. Load entity gazetteer from:
       - _brain_api/bid/_open.json  -> bid_ids, company names, clients
       - _agent_state/_registry.json -> agent_names
       - Hard-coded company aliases (Globex, Initech, Umbrella, etc.)
  2. For each memory string (recent_learnings, global_patterns, self_observations)
     scan for (entity, belief_type, value) via per-type regex:
       - stage:       enum words  [Discover, Qualify, Propose, Negotiate, Won, Lost]
       - value:       money amount [€/$/£ followed by digits with k/M/B suffix]
       - budget:      same money pattern when 'budget' in context window
       - deadline:    Q[1-4] YYYY or YYYY-MM-DD or date phrases
       - probability: NN% or NN percent
       - role:        known job title patterns (CFO, VP, Signer, etc.)
       - owner:       personal name patterns within 8 words of bid entity

  3. Pairwise compare all extracted beliefs for same (entity, belief_type):
       - Normalize values before comparing (case-fold, strip whitespace,
         canonical currency: strip EUR/€/$ prefix, parse numeric suffix k/M)
       - Different normalized values from two different agents -> ContradictionRecord

Contradiction severity:
  - high:   belief_type in {value, stage, deadline}
  - medium: belief_type in {budget, probability}
  - low:    belief_type in {role, owner}

Output:
  _brain_api/agent/contradictions.json  (always written; empty list when none found)
  Important/escalations/YYYY-MM-DD-contradiction-<id>.md  per HIGH contradiction
    (real runs only; suppressed under --root / --dry-run)

Idempotency:
  _agent_state/contradiction-detector/memory.json tracks emitted escalation ids;
  re-running never duplicates an escalation file.

Usage:
    python build/tools/contradiction_detector.py
    python build/tools/contradiction_detector.py --dry-run
    python build/tools/contradiction_detector.py --json
    python build/tools/contradiction_detector.py --root /tmp/test-vault --dry-run
    NO_LLM=1  # env flag (no-op here; all extraction is deterministic)
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_NAME = "contradiction-detector"
TOOL_VERSION = "1.0"

# Bid stage vocabulary (canonical casing)
STAGE_VOCAB = {"discover", "qualify", "propose", "negotiate", "won", "lost"}

# Belief types the tool can extract
BELIEF_TYPES = {"stage", "value", "budget", "deadline", "probability", "role", "owner"}

# Severity mapping
HIGH_TYPES = {"value", "stage", "deadline"}
MEDIUM_TYPES = {"budget", "probability"}
LOW_TYPES = {"role", "owner"}

# Role/title keywords for extraction
ROLE_TITLES = [
    "cfo", "cto", "coo", "ceo", "vp", "director", "manager",
    "signer", "budget owner", "procurement", "sponsor",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def severity(belief_type: str) -> str:
    if belief_type in HIGH_TYPES:
        return "high"
    if belief_type in MEDIUM_TYPES:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Gazetteer builder
# ---------------------------------------------------------------------------

# Hard-coded client alias banks.  Key = normalised canonical client name.
# Aliases are what agents write in free text.  These seed the gazetteer
# directly so entities mentioned without an active bid are still detected.
_COMPANY_ALIAS_BANKS: dict[str, list[str]] = {
    "globex": ["globex", "globex corp", "globex inc"],
    "initech": ["initech", "initech corp", "initech inc"],
    "umbrella": ["umbrella", "umbrella corp", "umbrella inc"],
}

# Mapping from lowercase alias -> canonical key (for deduplication)
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias.lower(): key
    for key, aliases in _COMPANY_ALIAS_BANKS.items()
    for alias in aliases
}


def build_gazetteer(root: Path) -> dict[str, list[str]]:
    """Return {canonical_entity_key: [alias, alias, ...]} mapping.

    An entity match fires when any alias appears (case-insensitive) in a
    belief string.  Deduplication ensures the same real-world client is
    never counted under two different canonical keys.

    Canonical key priority:
      1. client short-name from _COMPANY_ALIAS_BANKS (most readable in output)
      2. bid_id from _open.json when no alias-bank entry matches
      3. bid_id directly when no other match

    Sources:
      _brain_api/bid/_open.json  — bid_ids, company, client, topic
      _COMPANY_ALIAS_BANKS       — hard-coded known client aliases
    """
    # Start with all hard-coded alias banks as standalone entities
    entities: dict[str, set[str]] = {
        key: set(aliases)
        for key, aliases in _COMPANY_ALIAS_BANKS.items()
    }
    # Track which canonical key owns each alias (dedup map)
    # Hard-coded banks take precedence; bid aliases get merged if they match
    alias_owner: dict[str, str] = dict(_ALIAS_TO_CANONICAL)

    def _register(canonical: str, *raw: str) -> None:
        """Add aliases to canonical key, avoiding cross-contamination."""
        for a in raw:
            if not a or not a.strip():
                continue
            a_lc = a.lower().strip()
            existing_owner = alias_owner.get(a_lc)
            if existing_owner and existing_owner != canonical:
                # This alias already belongs to another canonical entity.
                # Do not add it here (would cause double-matching).
                continue
            entities.setdefault(canonical, set()).add(a_lc)
            alias_owner[a_lc] = canonical

    # From _open.json — bid_id aliases merged into existing canonical or new key
    open_path = root / "_brain_api" / "bid" / "_open.json"
    if open_path.exists():
        try:
            data = json.loads(open_path.read_text())
            for bid in data.get("bids", []):
                bid_id = bid.get("bid_id", "")
                company = bid.get("company", "")
                client = bid.get("client", "")
                topic = bid.get("topic", "")
                if not bid_id:
                    continue
                # Determine canonical key: prefer alias-bank key if client matches
                canonical = _ALIAS_TO_CANONICAL.get(client.lower().strip())
                if not canonical:
                    canonical = _ALIAS_TO_CANONICAL.get(company.lower().strip())
                if not canonical:
                    canonical = bid_id.lower()

                _register(canonical, bid_id, company, client, topic,
                          f"{company} {topic}")
        except (json.JSONDecodeError, KeyError):
            pass

    # Collapse to {key: sorted list of aliases}, longest first for greedy matching
    return {k: sorted(v, key=len, reverse=True) for k, v in entities.items()}


# ---------------------------------------------------------------------------
# Belief extraction (deterministic regex)
# ---------------------------------------------------------------------------


def _money_to_normalized(raw: str) -> str | None:
    """Normalize money string to float-as-string in euros (rough).

    Handles: €450k  $1.2M  £300k  1200000  2.5M  450,000
    Returns: e.g. '450000.0'
    """
    s = raw.lower().replace(",", "").replace(" ", "")
    s = re.sub(r"[€$£eur]", "", s)
    m = re.match(r"([\d.]+)\s*([kmb])?$", s)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2) or ""
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    elif suffix == "b":
        num *= 1_000_000_000
    return str(num)


# Money pattern: optional currency symbol, digits, optional k/M/B
_RE_MONEY = re.compile(
    r"[€$£]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|b|million|billion|thousand)?\b",
    re.IGNORECASE,
)

# Stage pattern
_RE_STAGE = re.compile(
    r"\b(discover|qualify|propose|negotiate|won|lost)\b", re.IGNORECASE
)

# Date patterns: YYYY-MM-DD or Q[1-4] YYYY
_RE_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|Q[1-4]\s*\d{4})\b", re.IGNORECASE
)

# Percentage
_RE_PCT = re.compile(r"\b(\d{1,3})\s*%")

# Role titles
_RE_ROLE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in ROLE_TITLES) + r")\b", re.IGNORECASE
)


def extract_beliefs(
    text: str,
    agent: str,
    ts: str,
    raw: str,
    gazetteer: dict[str, list[str]],
) -> list[dict]:
    """Extract (entity, belief_type, value) tuples from a single belief string.

    Uses the gazetteer to find entity mentions; for each found entity extracts
    all applicable belief signals from the full text.

    Returns list of dicts: {entity, belief_type, value, normalized, agent, raw, ts}
    """
    text_lc = text.lower()
    found: list[dict] = []
    matched_entities: list[str] = []

    for entity_key, aliases in gazetteer.items():
        for alias in aliases:
            if alias in text_lc:
                matched_entities.append(entity_key)
                break  # only count entity once per text

    if not matched_entities:
        return []

    for entity in matched_entities:
        # stage
        for m in _RE_STAGE.finditer(text):
            val = m.group(1).lower()
            found.append(_make_belief(entity, "stage", val, val, agent, raw, ts))

        # value — money amounts mentioned alongside entity
        for m in _RE_MONEY.finditer(text):
            raw_money = m.group(0).strip()
            norm = _money_to_normalized(raw_money)
            if norm and float(norm) > 999:  # skip tiny noise numbers
                # Decide: is this a budget mention or a deal value mention?
                window_start = max(0, m.start() - 40)
                window_end = min(len(text), m.end() + 40)
                context = text[window_start:window_end].lower()
                btype = "budget" if "budget" in context else "value"
                found.append(_make_belief(entity, btype, raw_money, norm, agent, raw, ts))

        # deadline
        for m in _RE_DATE.finditer(text):
            val = m.group(1)
            found.append(_make_belief(entity, "deadline", val, val.lower(), agent, raw, ts))

        # probability
        for m in _RE_PCT.finditer(text):
            val = m.group(1) + "%"
            found.append(_make_belief(entity, "probability", val, m.group(1), agent, raw, ts))

        # role
        for m in _RE_ROLE.finditer(text):
            val = m.group(1).upper()
            found.append(_make_belief(entity, "role", val, val.lower(), agent, raw, ts))

    return found


def _make_belief(
    entity: str,
    belief_type: str,
    value: str,
    normalized: str,
    agent: str,
    raw: str,
    ts: str,
) -> dict:
    return {
        "entity": entity,
        "belief_type": belief_type,
        "value": value,
        "normalized": normalized,
        "agent": agent,
        "raw": raw,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Memory file loading
# ---------------------------------------------------------------------------


def _belief_strings_from_memory(data: dict) -> list[tuple[str, str]]:
    """Yield (text, ts) from all free-text fields of a memory.json dict."""
    results: list[tuple[str, str]] = []

    def _item_text_ts(item: Any, default_ts: str) -> tuple[str, str] | None:
        if isinstance(item, str):
            return item, default_ts
        if isinstance(item, dict):
            text = item.get("text") or item.get("learning") or item.get("pattern") or ""
            ts = item.get("date") or item.get("ts") or default_ts
            if text:
                return str(text), str(ts)
        return None

    default_ts = str(data.get("last_updated", ""))

    for field in ("recent_learnings", "global_patterns", "self_observations",
                  "mistakes_to_avoid"):
        items = data.get(field, [])
        if isinstance(items, list):
            for item in items:
                result = _item_text_ts(item, default_ts)
                if result:
                    results.append(result)
        elif isinstance(items, str) and items:
            results.append((items, default_ts))

    # per_account_knowledge: dict of {account: [...]} or {account: str}
    pak = data.get("per_account_knowledge", {})
    if isinstance(pak, dict):
        for v in pak.values():
            if isinstance(v, list):
                for item in v:
                    result = _item_text_ts(item, default_ts)
                    if result:
                        results.append(result)
            elif isinstance(v, str) and v:
                results.append((v, default_ts))

    return results


def load_all_beliefs(
    root: Path,
    gazetteer: dict[str, list[str]],
) -> list[dict]:
    """Load all belief tuples from every _agent_state/<agent>/memory.json."""
    all_beliefs: list[dict] = []
    agent_state_dir = root / "_agent_state"
    if not agent_state_dir.is_dir():
        return all_beliefs

    for memory_path in sorted(agent_state_dir.glob("*/memory.json")):
        agent = memory_path.parent.name
        if agent.startswith("_"):
            continue  # skip _registry.json parent etc.
        try:
            data = json.loads(memory_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        for text, ts in _belief_strings_from_memory(data):
            beliefs = extract_beliefs(text, agent, ts, text, gazetteer)
            all_beliefs.extend(beliefs)

    return all_beliefs


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


def detect_contradictions(beliefs: list[dict]) -> list[dict]:
    """Pairwise compare beliefs with same (entity, belief_type) across agents.

    Returns list of ContradictionRecord dicts.
    """
    # Group by (entity, belief_type)
    groups: dict[tuple, list[dict]] = {}
    for b in beliefs:
        key = (b["entity"], b["belief_type"])
        groups.setdefault(key, []).append(b)

    records: list[dict] = []
    seen_pairs: set[frozenset] = set()  # avoid duplicate record pairs

    for (entity, btype), group in groups.items():
        # Deduplicate by (agent, normalized)
        unique: dict[str, list[dict]] = {}
        for b in group:
            unique.setdefault(b["agent"], []).append(b)

        agents = list(unique.keys())
        # Pairwise
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                ag_a = agents[i]
                ag_b = agents[j]
                pair_key = frozenset([ag_a, ag_b])

                beliefs_a = unique[ag_a]
                beliefs_b = unique[ag_b]

                # Collect all normalized values per agent
                norms_a = {b["normalized"] for b in beliefs_a}
                norms_b = {b["normalized"] for b in beliefs_b}

                # Contradiction = agents have at least one differing normalized value
                # AND no normalized value is shared
                shared = norms_a & norms_b
                if shared:
                    continue  # they agree on at least one value -> not a contradiction

                # Pick the most recent belief from each agent as representative
                rep_a = max(beliefs_a, key=lambda b: b["ts"])
                rep_b = max(beliefs_b, key=lambda b: b["ts"])

                # Make a stable record id
                record_id = _stable_id(entity, btype, ag_a, ag_b)

                # Avoid duplicate records for same pair+entity+type
                dedup_key = frozenset([record_id])
                if dedup_key in seen_pairs:
                    continue
                seen_pairs.add(dedup_key)

                records.append({
                    "id": record_id,
                    "entity": entity,
                    "belief_type": btype,
                    "severity": severity(btype),
                    "a": {
                        "agent": rep_a["agent"],
                        "value": rep_a["value"],
                        "normalized": rep_a["normalized"],
                        "raw": rep_a["raw"],
                        "ts": rep_a["ts"],
                    },
                    "b": {
                        "agent": rep_b["agent"],
                        "value": rep_b["value"],
                        "normalized": rep_b["normalized"],
                        "raw": rep_b["raw"],
                        "ts": rep_b["ts"],
                    },
                })

    return records


def _stable_id(entity: str, btype: str, ag_a: str, ag_b: str) -> str:
    """Deterministic id from components (alphabetical agent order)."""
    agents = sorted([ag_a, ag_b])
    raw = f"{entity}|{btype}|{agents[0]}|{agents[1]}"
    # simple stable hash using built-in hash with fixed seed via uuid5
    import hashlib
    digest = hashlib.sha1(raw.encode()).hexdigest()[:12]
    return f"cd-{digest}"


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def write_contradictions_json(
    output_path: Path,
    records: list[dict],
    meta: dict,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "generated": utcnow(),
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            **meta,
        },
        "contradictions": records,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def write_escalation_md(
    escalations_dir: Path,
    record: dict,
    date_str: str,
) -> Path:
    entity = record["entity"]
    btype = record["belief_type"]
    rec_id = record["id"]
    filename = f"{date_str}-contradiction-{rec_id}.md"
    path = escalations_dir / filename

    ag_a = record["a"]["agent"]
    ag_b = record["b"]["agent"]
    val_a = record["a"]["value"]
    val_b = record["b"]["value"]
    ts_a = record["a"]["ts"]
    ts_b = record["b"]["ts"]

    source_run_id = f"{TOOL_NAME}-{rec_id}-{date_str}T00:00:00Z"

    content = f"""---
sbap_version: "1.0"
source_agent: {TOOL_NAME}
source_run_id: "{source_run_id}"
generated: "{utcnow()}"
input_context_refs:
  - "_agent_state/{ag_a}/memory.json"
  - "_agent_state/{ag_b}/memory.json"
output_type: escalation_alert
target_path: "Important/escalations/{filename}"
confidence: 0.90
sensitivity: internal
needs_review: true
reasoning_summary: |
  Agents {ag_a} and {ag_b} hold conflicting beliefs about
  entity '{entity}' for belief_type '{btype}': '{val_a}' vs '{val_b}'.
---

# CONTRADICTION ({record['severity'].upper()}): {entity} / {btype}

**Record id:** `{rec_id}`
**Date detected:** {date_str}
**Severity:** {record['severity']}

## Conflicting beliefs

| Agent | Value | Timestamp | Raw text |
|---|---|---|---|
| `{ag_a}` | `{val_a}` | {ts_a} | {record['a']['raw'][:120]} |
| `{ag_b}` | `{val_b}` | {ts_b} | {record['b']['raw'][:120]} |

## Recommended actions

- Review both agents' source notes for `{entity}`
- Determine which belief reflects the current ground truth
- Update the stale agent's memory.json or add a correction note
- Resolve via `/dust-resolve` if the conflict originated from Dust writes

"""
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Heartbeat / idempotency memory
# ---------------------------------------------------------------------------

STATE_SCHEMA: dict = {
    "agent": TOOL_NAME,
    "memory_version": "1.0",
    "last_updated": "",
    "emitted_escalation_ids": [],
    "run_history": [],
}


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            # Ensure required keys exist
            for k, v in STATE_SCHEMA.items():
                if k not in data:
                    data[k] = v
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return dict(STATE_SCHEMA)


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = utcnow()
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Detect conflicting beliefs across agent memory.json files."
    )
    p.add_argument(
        "--root",
        default="",
        help="Override vault root (for testing). Default: auto-detect from cwd.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and print contradictions; write nothing.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit JSON to stdout instead of human-readable summary.",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="No-op (extraction is always deterministic). Accepted for CLI parity.",
    )
    return p.parse_args()


def find_vault_root() -> Path:
    """Walk up from cwd looking for CLAUDE.md marker; fall back to known path."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "CLAUDE.md").exists() and (candidate / "_agent_state").is_dir():
            return candidate
    # Hard-coded fallback (matches house convention)
    fallback = Path(os.environ.get("VAULT_ROOT", "")).resolve() if os.environ.get("VAULT_ROOT") else Path.cwd()
    if fallback.is_dir():
        return fallback
    return cwd


def main() -> int:
    args = parse_args()

    # ── Resolve vault root ───────────────────────────────────────────────────
    if args.root:
        root = Path(args.root).resolve()
    else:
        root = find_vault_root()

    is_real_run = not args.dry_run and not args.root

    # ── Build gazetteer ──────────────────────────────────────────────────────
    gazetteer = build_gazetteer(root)

    # ── Extract all beliefs ──────────────────────────────────────────────────
    all_beliefs = load_all_beliefs(root, gazetteer)
    n_memory_files = len(set(b["agent"] for b in all_beliefs))
    n_tuples = len(all_beliefs)

    # ── Detect contradictions ────────────────────────────────────────────────
    records = detect_contradictions(all_beliefs)

    # ── Load idempotency state ───────────────────────────────────────────────
    state_path = root / "_agent_state" / TOOL_NAME / "memory.json"
    state = load_state(state_path)
    emitted_ids: set[str] = set(state.get("emitted_escalation_ids", []))

    # ── Write contradictions.json ────────────────────────────────────────────
    output_path = root / "_brain_api" / "agent" / "contradictions.json"
    meta = {
        "vault_root": str(root),
        "dry_run": args.dry_run,
        "n_memory_files_scanned": n_memory_files,
        "n_belief_tuples_extracted": n_tuples,
        "n_contradictions": len(records),
    }

    if not args.dry_run:
        write_contradictions_json(output_path, records, meta)

    # ── Emit escalations for HIGH severity (real runs only) ──────────────────
    escalations_written: list[str] = []
    if is_real_run:
        escalations_dir = root / "Important" / "escalations"
        date_str = today_str()
        for rec in records:
            if rec["severity"] == "high" and rec["id"] not in emitted_ids:
                esc_path = write_escalation_md(escalations_dir, rec, date_str)
                emitted_ids.add(rec["id"])
                escalations_written.append(str(esc_path))

    # ── Update state ──────────────────────────────────────────────────────────
    if not args.dry_run:
        state["emitted_escalation_ids"] = sorted(emitted_ids)
        run_entry = {
            "ts": utcnow(),
            "n_tuples": n_tuples,
            "n_contradictions": len(records),
            "dry_run": args.dry_run,
        }
        state.setdefault("run_history", []).append(run_entry)
        # Keep only last 50 runs
        state["run_history"] = state["run_history"][-50:]
        save_state(state_path, state)

    # ── Output ────────────────────────────────────────────────────────────────
    if args.json_output:
        out = {
            "n_memory_files_scanned": n_memory_files,
            "n_belief_tuples_extracted": n_tuples,
            "n_contradictions": len(records),
            "contradictions": records,
            "escalations_written": escalations_written,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"[{TOOL_NAME}] vault={root}")
        print(f"  Memory files scanned : {n_memory_files}")
        print(f"  Belief tuples extracted: {n_tuples}")
        print(f"  Contradictions found : {len(records)}")
        if records:
            print()
            for r in records:
                print(f"  [{r['severity'].upper()}] {r['entity']} / {r['belief_type']}")
                print(f"    {r['a']['agent']}: {r['a']['value']!r}")
                print(f"    {r['b']['agent']}: {r['b']['value']!r}")
                print(f"    id={r['id']}")
                print()
        else:
            print("  Result: PASS — no contradictions detected")
        if not args.dry_run:
            print(f"  Written: {output_path}")
        if escalations_written:
            print(f"  Escalations written: {len(escalations_written)}")
            for p in escalations_written:
                print(f"    {p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
