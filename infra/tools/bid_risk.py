#!/usr/bin/env python3
"""bid_risk.py — loss-antibody → risk_score.json (Phase 2, HS-05).

bid-qualifier calls this at qualify time. It reads matching loss antibodies and
docks the qualification score, citing the original lost bid:
  risk_score.json{bid_id, base_score, adjusted_score, matched_antibodies[], generated}

Antibody sources (in precedence order):
  1. PROMOTED canonical failure_mode blocks: _agent_state/canonical/failure_mode/*.json
     (durable; written only after block-curator triage approves a DRAFT).
  2. HELD DRAFTS: 00_Inbox/from-dust/close_bid/*.md (output_type=failure_mode).
     Drafts are matched ONLY when include_drafts=True (e.g. an early-warning preview);
     they NEVER auto-dock a real qualification until promoted. Default False.

Specificity guard (PLAN step 5): an antibody matches a bid ONLY if it carries
>= MIN_ANTIBODY_FILTERS concrete filters (specificity_ok) AND at least one filter
actually matches the bid context. This stops a vague antibody docking every bid.

Each match docks DOCK_PER_ANTIBODY (capped at DOCK_CAP) — co-occurrence signal,
honest about not being causal.

Usage (library):
    from bid_risk import write_risk_score
    write_risk_score("some-bid", base_score=72, bid_ctx={"vertical":"pharma","client":"X"})
CLI (synthetic test):
    python3 build/tools/bid_risk.py <bid-id> --base-score 72 --vertical pharma --client X \
        [--include-drafts]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canonical_lib import VAULT, VIEW, utcnow  # noqa: E402

ANTIBODY_SOURCE = VAULT / "_agent_state" / "canonical" / "failure_mode"
ANTIBODY_DRAFTS = VAULT / "00_Inbox" / "from-dust" / "close_bid"

MIN_ANTIBODY_FILTERS = 2     # mirrors close_bid.py — specificity guard
DOCK_PER_ANTIBODY = 8        # points docked per matching antibody
DOCK_CAP = 25                # max total dock (don't zero out a bid on antibodies alone)


def _filter_values(applicable_to: dict) -> dict[str, list]:
    """Extract the concrete filter lists (verticals/clients/service_lines), ignoring
    the bookkeeping keys."""
    out = {}
    for k in ("verticals", "clients", "service_lines", "industries"):
        v = applicable_to.get(k)
        if isinstance(v, list) and v:
            out[k] = [str(x).lower() for x in v]
    return out


def _ctx_values(bid_ctx: dict) -> dict[str, str]:
    out = {}
    if bid_ctx.get("vertical"):
        out["verticals"] = str(bid_ctx["vertical"]).lower()
        out["industries"] = str(bid_ctx["vertical"]).lower()
    if bid_ctx.get("client"):
        out["clients"] = str(bid_ctx["client"]).lower()
    if bid_ctx.get("service_line"):
        out["service_lines"] = str(bid_ctx["service_line"]).lower()
    return out


def _matches(applicable_to: dict, bid_ctx: dict) -> tuple[bool, list[str]]:
    """Specificity-guarded match. Returns (matched, matched_dimensions)."""
    n_filters = applicable_to.get("n_filters")
    if n_filters is None:
        n_filters = len(_filter_values(applicable_to))
    if n_filters < MIN_ANTIBODY_FILTERS or not applicable_to.get("specificity_ok", n_filters >= MIN_ANTIBODY_FILTERS):
        return False, []  # too broad — guard refuses the match
    filters = _filter_values(applicable_to)
    ctx = _ctx_values(bid_ctx)
    hits = []
    for dim, values in filters.items():
        cv = ctx.get(dim)
        if cv and cv in values:
            hits.append(dim)
    return (len(hits) > 0), hits


def _load_promoted_antibodies() -> list[dict]:
    out = []
    if ANTIBODY_SOURCE.exists():
        for p in sorted(ANTIBODY_SOURCE.glob("*.json")):
            if p.name.startswith("_") or p.name.endswith(".bak"):
                continue
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def _load_draft_antibodies() -> list[dict]:
    out = []
    if ANTIBODY_DRAFTS.exists():
        for p in sorted(ANTIBODY_DRAFTS.glob("*.md")):
            try:
                text = p.read_text()
            except OSError:
                continue
            # body is a ```json fenced block after the frontmatter
            start = text.find("```json")
            if start == -1:
                continue
            start = text.find("\n", start) + 1
            endf = text.find("```", start)
            if endf == -1:
                continue
            try:
                body = json.loads(text[start:endf])
            except json.JSONDecodeError:
                continue
            if body.get("type") == "failure_mode":
                body["_draft"] = True
                out.append(body)
    return out


def match_antibodies(bid_ctx: dict, include_drafts: bool = False) -> list[dict]:
    antibodies = _load_promoted_antibodies()
    if include_drafts:
        antibodies += _load_draft_antibodies()
    matched = []
    for ab in antibodies:
        ok, dims = _matches(ab.get("applicable_to", {}), bid_ctx)
        if not ok:
            continue
        ev = (ab.get("evidence") or [{}])[0]
        matched.append({
            "antibody_key": ab.get("key"),
            "lost_bid": ev.get("bid_id"),
            "loss_reason": ev.get("loss_reason"),
            "matched_dimensions": dims,
            "is_draft": bool(ab.get("_draft")),
            "dock": DOCK_PER_ANTIBODY,
        })
    return matched


def write_risk_score(bid_id: str, base_score: float, bid_ctx: dict,
                     include_drafts: bool = False, dry_run: bool = False) -> dict:
    matched = match_antibodies(bid_ctx, include_drafts=include_drafts)
    total_dock = min(DOCK_CAP, DOCK_PER_ANTIBODY * len(matched))
    adjusted = max(0.0, base_score - total_dock)
    out = {
        "bid_id": bid_id,
        "base_score": base_score,
        "adjusted_score": adjusted,
        "total_dock": total_dock,
        "matched_antibodies": matched,
        "include_drafts": include_drafts,
        "generated": utcnow(),
        "note": "Antibody match is CO-OCCURRENCE risk (prior similar bids lost), not "
                "causal proof. Drafts (is_draft:true) are previews — promote via "
                "block-curator before they should affect a real qualification.",
    }
    if not dry_run:
        bid_dir = VIEW.parent / "bid" / bid_id
        bid_dir.mkdir(parents=True, exist_ok=True)
        (bid_dir / "risk_score.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid_id")
    ap.add_argument("--base-score", type=float, default=70.0)
    ap.add_argument("--vertical")
    ap.add_argument("--client")
    ap.add_argument("--service-line")
    ap.add_argument("--include-drafts", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ctx = {"vertical": args.vertical, "client": args.client, "service_line": args.service_line}
    out = write_risk_score(args.bid_id, args.base_score, ctx,
                           include_drafts=args.include_drafts, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
