#!/usr/bin/env python3
"""ingest_debrief.py — validate a debrief.md against the debrief/1.0 schema and
extract the loss_reason for downstream use.

Part of Phase 3, item #4 of the Holy-Shit 20 build.
Integration contract is documented in:
  _Skills/01 Sales/bid-debrief/SKILL.md  (§ Integration Contract)

Usage:
    python3 build/tools/ingest_debrief.py <path-to-debrief.md>
    python3 build/tools/ingest_debrief.py <path-to-debrief.md> --extract-loss-reason
    python3 build/tools/ingest_debrief.py <path-to-debrief.md> --check-ledger-consistency

Exit codes:
    0 — schema valid (warnings may still be printed)
    1 — schema invalid (required fields missing or wrong types)
    2 — file not found or not parseable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# ─── Schema definition ────────────────────────────────────────────────────────

SCHEMA_VERSION = "debrief/1.0"

# Required fields: (field_name, allowed_types)
REQUIRED = [
    ("schema_version", (str,)),
    ("bid_id", (str,)),
    ("outcome", (str,)),
    ("debrief_date", (str,)),
    ("ingested_date", (str,)),
    ("source", (str,)),
    ("confidence", (str,)),
]

VALID_OUTCOMES = {"won", "lost", "no-decision"}
VALID_CONFIDENCE = {"high", "medium", "low"}

# Fields that should carry loss_reason on a LOST bid
LOSS_REASON_FIELD = "loss_reason"


# ─── Parser ───────────────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Raises ValueError if no frontmatter."""
    if not text.startswith("---\n"):
        raise ValueError("File does not start with YAML frontmatter (expected '---\\n')")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("Unclosed YAML frontmatter (no closing '---')")
    raw_yaml = text[4:end]
    body = text[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in frontmatter: {e}") from e
    return fm, body


# ─── Validation ───────────────────────────────────────────────────────────────

def validate(fm: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors = schema violations; warnings = best-practice gaps."""
    errors: list[str] = []
    warnings: list[str] = []

    # schema_version must match
    sv = fm.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(f"schema_version must be '{SCHEMA_VERSION}', got: {sv!r}")

    # Required fields
    for field, types in REQUIRED:
        val = fm.get(field)
        if val is None:
            errors.append(f"Required field missing or null: {field}")
        elif not isinstance(val, types):
            errors.append(f"Field '{field}' has wrong type (expected {types}, got {type(val).__name__})")

    # outcome must be a known value
    outcome = fm.get("outcome")
    if outcome is not None and outcome not in VALID_OUTCOMES:
        errors.append(f"'outcome' must be one of {VALID_OUTCOMES}, got: {outcome!r}")

    # confidence must be a known value
    conf = fm.get("confidence")
    if conf is not None and conf not in VALID_CONFIDENCE:
        errors.append(f"'confidence' must be one of {VALID_CONFIDENCE}, got: {conf!r}")

    # loss_reason check on LOST bids
    if outcome == "lost":
        lr = fm.get(LOSS_REASON_FIELD)
        if not lr:
            warnings.append(
                "LOST bid with no loss_reason — the loss antibody (HS-05) will be empty. "
                "Populate loss_reason: in frontmatter and copy to 03 - Decision Log.md."
            )

    # criteria should be a non-empty list
    criteria = fm.get("criteria")
    if not criteria:
        warnings.append(
            "No criteria entries — rubric calibration (HS-03) will have no weight signal."
        )
    elif isinstance(criteria, list):
        for i, c in enumerate(criteria):
            if not isinstance(c, dict):
                errors.append(f"criteria[{i}] is not a mapping")
            elif c.get("criterion") is None:
                warnings.append(f"criteria[{i}].criterion is null — consider using a placeholder name")

    # confirmed_by_bid_manager should be set
    confirmed = fm.get("confirmed_by_bid_manager")
    if confirmed is False or confirmed is None:
        warnings.append(
            "confirmed_by_bid_manager is not true — debrief is not yet ground truth. "
            "Have the Bid Manager review and set to true."
        )

    # feeds_* flags
    for flag in ("feeds_ledger_reconcile", "feeds_antibody_loss_reason", "feeds_rubric_calibration"):
        if fm.get(flag) is not True:
            warnings.append(f"'{flag}' is not true — downstream feed flag should be explicitly set.")

    return errors, warnings


# ─── Ledger consistency check ─────────────────────────────────────────────────

def check_ledger_consistency(fm: dict) -> list[str]:
    """Read _agent_state/outcome-ledger.jsonl and check if any ledger entry for this
    bid_id has a different outcome than the debrief.  Returns a list of findings."""
    vault = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
    ledger = vault / "_agent_state" / "outcome-ledger.jsonl"
    if not ledger.exists():
        return ["Ledger not found at _agent_state/outcome-ledger.jsonl — no consistency check possible."]

    bid_id = fm.get("bid_id")
    debrief_outcome = fm.get("outcome")
    findings: list[str] = []
    conflicting_blocks: list[str] = []

    with ledger.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("bid_id") == bid_id:
                ledger_outcome = rec.get("outcome")
                if ledger_outcome != debrief_outcome:
                    conflicting_blocks.append(
                        f"  block_key={rec.get('block_key')} "
                        f"ledger_outcome={ledger_outcome} vs debrief_outcome={debrief_outcome}"
                    )

    if conflicting_blocks:
        findings.append(
            f"CONFLICT: ledger has different outcome(s) for bid_id='{bid_id}' "
            f"than this debrief (debrief is ground truth — re-run close_bid.py):"
        )
        findings.extend(conflicting_blocks)
    else:
        # Check if any ledger entries exist at all
        bid_found = False
        with ledger.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("bid_id") == bid_id:
                    bid_found = True
                    break
        if bid_found:
            findings.append(f"OK: ledger entries for bid_id='{bid_id}' are consistent with debrief outcome='{debrief_outcome}'.")
        else:
            findings.append(f"INFO: no ledger entries found for bid_id='{bid_id}' — close_bid.py has not run yet.")

    return findings


# ─── Extract helpers ──────────────────────────────────────────────────────────

def extract_loss_reason(fm: dict) -> dict[str, Any]:
    """Return a dict suitable for copying into 03 - Decision Log.md frontmatter."""
    return {
        "loss_reason": fm.get(LOSS_REASON_FIELD),
        "bid_id": fm.get("bid_id"),
        "named_objections": fm.get("named_objections") or [],
        "why_they_chose_winner": fm.get("why_they_chose_winner"),
        "debrief_source": fm.get("source"),
        "debrief_confidence": fm.get("confidence"),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate a debrief.md and extract loss_reason for close_bid.py."
    )
    ap.add_argument("debrief_path", help="Path to debrief.md")
    ap.add_argument(
        "--extract-loss-reason",
        action="store_true",
        help="Print the loss_reason dict (for copying to 03 - Decision Log.md)",
    )
    ap.add_argument(
        "--check-ledger-consistency",
        action="store_true",
        help="Compare debrief outcome against _agent_state/outcome-ledger.jsonl",
    )
    args = ap.parse_args()

    path = Path(args.debrief_path)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    try:
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    errors, warnings = validate(fm)

    # Print results
    print(f"ingest_debrief: {path}")
    print(f"  bid_id   : {fm.get('bid_id')}")
    print(f"  outcome  : {fm.get('outcome')}")
    print(f"  confidence: {fm.get('confidence')}")
    print(f"  source   : {fm.get('source')}")
    print()

    if errors:
        print(f"SCHEMA ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  ERROR: {e}")
    else:
        print("SCHEMA: VALID")

    if warnings:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  WARN: {w}")
    else:
        print("WARNINGS: none")

    if args.extract_loss_reason:
        lr = extract_loss_reason(fm)
        print("\n--- loss_reason extraction (copy to 03 - Decision Log.md frontmatter) ---")
        print(yaml.dump(lr, allow_unicode=True, sort_keys=False).strip())

    if args.check_ledger_consistency:
        print("\n--- ledger consistency check ---")
        for finding in check_ledger_consistency(fm):
            print(f"  {finding}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
