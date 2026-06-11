#!/usr/bin/env python3
"""build_canonical_view.py — fold the outcome ledger into the canonical VIEW.

Phase 2 (the outcome-ledger spine). SBAP v1.1.

Durability architecture (PLAN.md §Durability):
  SOURCE (durable, git-tracked):
    _agent_state/canonical/<type>/<key>.json   — block bodies
    _agent_state/outcome-ledger.jsonl          — append-only outcomes
  VIEW (regenerable, gitignored under _brain_api/):
    _brain_api/canonical/<type>/<key>.json     — body + computed performance{}

This script reads every SOURCE block, folds the ledger (keyed by
(bid_id, block_key); last updated_at wins on read-compaction), computes the v1.1
performance{} object, and (re)writes the VIEW. It runs in brain-refresh.sh AFTER
build_brain_api.py (which scaffolds the empty _brain_api/canonical/ dirs).

Round-trip guarantee: delete any _brain_api view block, run this, and it
regenerates from the _agent_state source — fixing the latent durability bug where
the 3 hand-written blocks lived ONLY in gitignored _brain_api/.

Usage:
    python3 build/tools/build_canonical_view.py
    python3 build/tools/build_canonical_view.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canonical_lib import (  # noqa: E402
    VAULT, SOURCE, LEDGER, VIEW, MIN_OUTCOMES_FOR_RATE, utcnow,
)

WILSON_Z = 1.96                    # 95% confidence — Wilson lower-bound z-score


def wilson_lower_bound(n_won: int, n: int, z: float = WILSON_Z) -> float:
    """Wilson score interval LOWER bound. Penalises small samples so a lucky 1/1
    never outranks a solid 7/8. Returns 0.0 for n==0 (defensive; callers guard on
    rankable so this is never the ranking value below MIN_OUTCOMES_FOR_RATE)."""
    if n <= 0:
        return 0.0
    p = n_won / n
    denom = 1.0 + (z * z) / n
    centre = p + (z * z) / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _ledger_key(rec: dict) -> tuple[str, str]:
    return (str(rec.get("bid_id", "")), str(rec.get("block_key", "")))


def fold_ledger() -> dict[str, list[dict]]:
    """Read the append-only ledger and compact by (bid_id, block_key): last
    updated_at wins (re-close updates an outcome, never duplicates it). Returns a
    map block_key -> [compacted outcome records that used this block]."""
    if not LEDGER.exists():
        return {}
    # compaction: keep the record with the max updated_at per (bid_id, block_key)
    compacted: dict[tuple[str, str], dict] = {}
    with LEDGER.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line; never crash the fold
            k = _ledger_key(rec)
            prev = compacted.get(k)
            if prev is None or str(rec.get("updated_at", "")) >= str(prev.get("updated_at", "")):
                compacted[k] = rec
    by_block: dict[str, list[dict]] = {}
    for (_, block_key), rec in compacted.items():
        by_block.setdefault(block_key, []).append(rec)
    return by_block


def compute_performance(block_key: str, outcomes: list[dict]) -> dict:
    """Compute the v1.1 performance{} object from this block's compacted outcomes.
    CO-OCCURRENCE not causation. n_outcomes counts DISTINCT bids (compaction already
    deduped by (bid_id, block_key); a block appears once per bid)."""
    n_outcomes = len(outcomes)
    n_won = sum(1 for o in outcomes if str(o.get("outcome", "")).lower() == "won")
    rankable = n_outcomes >= MIN_OUTCOMES_FOR_RATE
    win_rate = (n_won / n_outcomes) if rankable and n_outcomes > 0 else None
    score = wilson_lower_bound(n_won, n_outcomes) if rankable else None
    # value_weighted_eligible: false if ANY contributing outcome lacks usable
    # value_eur (null/0) or vertical (PLAN Q3). Conservative.
    value_weighted_eligible = bool(outcomes) and all(
        (o.get("value_eur") not in (None, 0, 0.0)) and bool(o.get("vertical"))
        for o in outcomes
    )
    last_used = None
    dates = [str(o.get("date", "")) for o in outcomes if o.get("date")]
    if dates:
        last_used = max(dates)
    return {
        "n_outcomes": n_outcomes,
        "n_won": n_won,
        "win_rate": win_rate,
        "score": score,
        "rankable": rankable,
        "value_weighted_eligible": value_weighted_eligible,
        "last_used": last_used,
        "computed_at": utcnow(),
    }


def default_performance() -> dict:
    return {
        "n_outcomes": 0, "n_won": 0, "win_rate": None, "score": None,
        "rankable": False, "value_weighted_eligible": False,
        "last_used": None, "computed_at": utcnow(),
    }


def iter_source_blocks():
    """Yield (type_dir_name, key, source_dict, source_path) for each source block."""
    if not SOURCE.exists():
        return
    for type_dir in sorted(SOURCE.iterdir()):
        if not type_dir.is_dir() or type_dir.name.startswith("_"):
            continue
        for src_path in sorted(type_dir.glob("*.json")):
            if src_path.name.startswith("_") or src_path.name.endswith(".bak"):
                continue
            try:
                data = json.loads(src_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"  ⚠ skip unreadable source {src_path.name}: {e}", file=sys.stderr)
                continue
            key = data.get("key") or src_path.stem
            yield type_dir.name, key, data, src_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    by_block = fold_ledger()
    n_written = 0
    n_with_outcomes = 0

    for type_name, key, data, src_path in iter_source_blocks():
        outcomes = by_block.get(key, [])
        perf = compute_performance(key, outcomes) if outcomes else default_performance()
        if outcomes:
            n_with_outcomes += 1
        view = dict(data)                 # copy the durable source body verbatim
        view["performance"] = perf        # overwrite with computed performance
        view["_generated_view"] = True    # mark: this is a GENERATED view, never a source
        view["_view_generated_at"] = perf["computed_at"]
        view["_source"] = f"_agent_state/canonical/{type_name}/{src_path.name}"

        out_dir = VIEW / type_name
        out_path = out_dir / f"{key}.json"
        if args.dry_run:
            print(f"DRY  would write {out_path.relative_to(VAULT)} "
                  f"(n_outcomes={perf['n_outcomes']}, rankable={perf['rankable']}, "
                  f"win_rate={perf['win_rate']}, score={perf['score']})")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(view, indent=2) + "\n")
        n_written += 1
        print(f"  view {out_path.relative_to(VAULT)}  "
              f"n={perf['n_outcomes']} won={perf['n_won']} "
              f"rankable={perf['rankable']} win_rate={perf['win_rate']} score={perf['score']}")

    if not args.dry_run:
        print(f"Folded canonical view: {n_written} block(s) written, "
              f"{n_with_outcomes} with ledger outcomes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
