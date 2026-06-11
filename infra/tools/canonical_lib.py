#!/usr/bin/env python3
"""canonical_lib.py — shared helpers for the Phase 2 outcome-ledger spine.

Used by build_canonical_view.py, close_bid.py, win_patterns.py. Centralises the
paths and the pull-stamp shape so the schema lives in exactly one place.

Layers (PLAN.md §Durability):
  SOURCE: _agent_state/canonical/<type>/<key>.json  (git-tracked block bodies)
          _agent_state/outcome-ledger.jsonl         (git-tracked, append-only)
  VIEW:   _brain_api/canonical/<type>/<key>.json    (regenerable; build_canonical_view.py)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
SOURCE = VAULT / "_agent_state" / "canonical"
LEDGER = VAULT / "_agent_state" / "outcome-ledger.jsonl"
VIEW = VAULT / "_brain_api" / "canonical"

MIN_OUTCOMES_FOR_RATE = 3


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_view_block(block_key: str) -> dict | None:
    """Find a generated VIEW block by its key across all type dirs. Returns the
    parsed dict (incl. computed performance{}) or None. The view is what carries
    fresh win_rate/n_outcomes/rankable, so the pull-stamp reads from here."""
    if not VIEW.exists():
        return None
    for type_dir in VIEW.iterdir():
        if not type_dir.is_dir():
            continue
        p = type_dir / f"{block_key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
    return None


def load_source_block(block_key: str) -> tuple[dict | None, Path | None]:
    """Find a SOURCE block by key. Returns (dict, path) or (None, None)."""
    if not SOURCE.exists():
        return None, None
    for type_dir in SOURCE.iterdir():
        if not type_dir.is_dir() or type_dir.name.startswith("_"):
            continue
        p = type_dir / f"{block_key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text()), p
            except (OSError, json.JSONDecodeError):
                return None, None
    return None, None


def make_pull_stamp(block_key: str, recommended_score: float = 0.0) -> dict:
    """Build one recommended_blocks.json entry for a block at pull time.

    Shape (PLAN.md step 2): {key, type, recommended_score, win_rate, n_outcomes,
    rankable, pulled_at, used}. `used` ALWAYS defaults FALSE — a block is credited
    only after explicit confirmation (close_bid.py prompt or Decision Log
    used_blocks:[...]). Prevents crediting recommended-but-cut blocks.
    win_rate/n_outcomes/rankable snapshot the VIEW's computed performance at pull
    time."""
    view = load_view_block(block_key) or {}
    perf = view.get("performance", {})
    return {
        "key": block_key,
        "type": view.get("type", "unknown"),
        "recommended_score": recommended_score,
        "win_rate": perf.get("win_rate"),
        "n_outcomes": perf.get("n_outcomes", 0),
        "rankable": perf.get("rankable", False),
        "pulled_at": utcnow(),
        "used": False,
    }


def write_recommended_blocks(bid_id: str, selected: list[dict | str]) -> Path:
    """Write _brain_api/bid/<bid-id>/recommended_blocks.json with the pull-stamp
    shape for each selected block. `selected` items may be a bare block_key string
    or {"key":..., "recommended_score":...}. used defaults FALSE for all.

    NOTE: _brain_api/bid/ is a generated view (gitignored). The durable record of
    which blocks were USED is the Decision Log used_blocks:[...] + the ledger.
    """
    stamps = []
    for item in selected:
        if isinstance(item, str):
            stamps.append(make_pull_stamp(item))
        else:
            stamps.append(make_pull_stamp(item["key"], float(item.get("recommended_score", 0.0))))
    out = {
        "bid_id": bid_id,
        "generated": utcnow(),
        "schema": "recommended_blocks.v1.1 (pull-stamp; used defaults false)",
        "blocks": stamps,
    }
    bid_dir = VIEW.parent / "bid" / bid_id
    bid_dir.mkdir(parents=True, exist_ok=True)
    p = bid_dir / "recommended_blocks.json"
    p.write_text(json.dumps(out, indent=2) + "\n")
    return p
