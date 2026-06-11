#!/usr/bin/env python3
"""close_bid.py — the close hook for the outcome-ledger spine (Phase 2).

On a bid's stage → Won|Lost:
  1. Resolve which recommended blocks were actually USED (used:true) — from the
     Decision Log `used_blocks:[...]` frontmatter (the explicit confirmation), with
     recommended_blocks.json as the candidate set. `used` defaults FALSE.
  2. For each used block append {bid_id, block_key, outcome, vertical, value_eur,
     date, updated_at} to _agent_state/outcome-ledger.jsonl — keyed by
     (bid_id, block_key): re-close appends a newer updated_at that the fold's
     last-updated-at compaction collapses to one (idempotent, never duplicates).
  3. REUSES tx_begin/tx_commit + the (content_hash, source_run_id) seen-set from
     triage_dust_writes.py for atomic, crash-visible, replay-suppressed appends.
  4. On LOST: write a DRAFT type=failure_mode antibody to
     00_Inbox/from-dust/close_bid/ (valid SBAP frontmatter, min-N specificity guard)
     — routed through normal block-curator triage, NEVER into _brain_api directly.
  5. Run build_canonical_view.py to refold + regenerate the view.

Usage:
    python3 build/tools/close_bid.py <bid-id>
    python3 build/tools/close_bid.py <bid-id> --dry-run
    # test overrides (synthetic bids that aren't in the vault):
    python3 build/tools/close_bid.py synthetic-spine-test \
        --outcome won --vertical pharma --value-eur 250000 \
        --used-blocks pharma__pricing_inquiry__response,partner_cosell__cto_outreach_pattern
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canonical_lib import VAULT, LEDGER, load_source_block, utcnow  # noqa: E402

# REUSE the battle-tested tx markers + idempotency seen-set from the triage script.
import triage_dust_writes as triage  # noqa: E402

INBOX = VAULT / "00_Inbox" / "from-dust" / "close_bid"
RFP_ROOT = VAULT / "RFPs"
LEDGER_AGENT = "close-bid"          # schema-valid agent name (^[a-z0-9-]+$) for tx/seen routing

# Loss antibody specificity guard (PLAN step 5): an antibody must carry at least
# this many concrete applicable_to filters, else it could dock every future bid.
MIN_ANTIBODY_FILTERS = 2


# ─────────────────────────── bid resolution ───────────────────────────

def find_bid_dir(bid_id: str) -> Path | None:
    """Locate RFPs/<...>/<Opp>/ whose folder slug matches bid_id."""
    if not RFP_ROOT.exists():
        return None
    for brief in RFP_ROOT.rglob("00 - Brief.md"):
        if brief.parent.name.lower().replace(" ", "-") == bid_id:
            return brief.parent
    return None


def parse_fm(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}


def _load_debrief(bid_dir: Path) -> dict:
    """The buyer debrief (bid-debrief skill, Phase 3 #4) is EXTERNAL ground truth —
    what the buyer actually told us. It overrides the Brief/Decision Log, but ONLY
    once a human has confirmed it (`confirmed_by_bid_manager: true`); an unconfirmed
    debrief never silently overrides. May sit in the bid folder at close, or in
    04_Archives/<slug>/ post-archive."""
    slug = bid_dir.name.lower().replace(" ", "-")
    for cand in (bid_dir / "debrief.md", VAULT / "04_Archives" / slug / "debrief.md"):
        if cand.exists():
            fm = parse_fm(cand.read_text())
            if fm.get("confirmed_by_bid_manager") is True:
                return fm
    return {}


def read_bid_context(bid_dir: Path) -> dict:
    """Pull outcome + vertical + value + used_blocks + loss_reason from the bid's
    Brief and Decision Log. Decision Log used_blocks:[...] is the explicit
    confirmation that credits a block (recommended != used). A CONFIRMED buyer
    debrief overrides outcome / value_eur / loss_reason (external ground truth)."""
    brief = parse_fm((bid_dir / "00 - Brief.md").read_text())
    dlog_path = bid_dir / "03 - Decision Log.md"
    dlog = parse_fm(dlog_path.read_text()) if dlog_path.exists() else {}
    deb = _load_debrief(bid_dir)
    stage = str(brief.get("stage", "")).lower()
    outcome = "won" if stage == "won" else ("lost" if stage == "lost" else None)
    if deb.get("outcome") in ("won", "lost"):
        outcome = deb["outcome"]          # debrief (confirmed) wins over stage
    return {
        "outcome": outcome,
        "vertical": brief.get("sector") or brief.get("service_line") or dlog.get("vertical"),
        "value_eur": deb.get("contract_value_eur") or deb.get("value_eur") or brief.get("value"),
        "used_blocks": list(dlog.get("used_blocks") or []),
        "loss_reason": deb.get("loss_reason") or dlog.get("loss_reason"),
        "client": brief.get("company") or brief.get("client"),
        "debrief_confirmed": bool(deb),
    }


# ─────────────────────────── ledger append ───────────────────────────

# Fields that define a logical outcome (everything except the timestamp). Two records
# with identical stable fields for the same (bid_id, block_key) are the same logical
# fact — re-appending one is a duplicate.
_STABLE_FIELDS = ("bid_id", "block_key", "outcome", "vertical", "value_eur", "date")


def _stable_view(rec: dict) -> dict:
    return {k: rec.get(k) for k in _STABLE_FIELDS}


def latest_ledger_record(bid_id: str, block_key: str) -> dict | None:
    """Return the most-recent (max updated_at) ledger record for (bid_id, block_key),
    or None. The ledger IS the idempotency source of truth — we never duplicate the
    CURRENT-latest fact, but a genuine outcome FLIP (won→lost) is still allowed."""
    if not LEDGER.exists():
        return None
    latest = None
    with LEDGER.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("bid_id") == bid_id and r.get("block_key") == block_key:
                if latest is None or str(r.get("updated_at", "")) >= str(latest.get("updated_at", "")):
                    latest = r
    return latest


def append_outcome(bid_id: str, block_key: str, outcome: str, vertical, value_eur,
                   date: str, seen: dict, dry_run: bool = False) -> str:
    """Append one outcome to the ledger — atomic, crash-visible, idempotent.

    Idempotency (single source of truth = the ledger's CURRENT-latest record for
    this (bid_id, block_key)):
      - If the latest record already carries these exact stable facts → suppress
        (a re-close with no change is a duplicate). This agrees with the fold's
        last-updated-at compaction by construction — no seen-set/compaction divergence.
      - A genuine outcome FLIP (e.g. won→lost, or back) appends a newer record; the
        fold keeps only the latest. Never duplicates the current fact.
    tx_begin/tx_commit (reused from triage_dust_writes.py) make the append
    crash-visible. The seen-set is bumped too, for fleet-wide audit symmetry, but the
    ledger — not the seen-set — is authoritative for suppression.
    """
    rec = {
        "bid_id": bid_id,
        "block_key": block_key,
        "outcome": outcome,
        "vertical": vertical,
        "value_eur": value_eur,
        "date": date,
        "updated_at": utcnow(),
    }
    latest = latest_ledger_record(bid_id, block_key)
    if latest is not None and _stable_view(latest) == _stable_view(rec):
        triage.record_replay_suppressed(LEDGER_AGENT, dry_run=dry_run)
        return f"REPLAY_SUPPRESSED {block_key}: latest ledger fact already == {outcome} (no change)"

    if dry_run:
        return f"DRY append {block_key}: {outcome} (vertical={vertical}, value_eur={value_eur})"

    # seen-set key over the stable facts — audit symmetry with triage; not authoritative.
    stable = _stable_view(rec)
    c_hash = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    run_id = f"close-{bid_id}-{block_key}-{outcome}"

    marker = triage.tx_begin(LEDGER_AGENT, {"op": "ledger_append", "bid": bid_id,
                                            "block": block_key, "outcome": outcome})
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        triage.tx_commit(marker)
    except OSError as e:
        triage.log_error(f"ledger append failed: {bid_id}/{block_key}: {e} (marker stays)")
        raise
    triage.mark_seen(LEDGER_AGENT, c_hash, run_id, seen, dry_run=dry_run)
    return f"APPENDED {block_key}: {outcome} (vertical={vertical}, value_eur={value_eur})"


# ─────────────────────────── loss antibody ───────────────────────────

def build_antibody_applicable_to(vertical, client, ctx: dict) -> tuple[dict, list[str]]:
    """Construct applicable_to with a min-N specificity guard. Returns
    (applicable_to, warnings). If fewer than MIN_ANTIBODY_FILTERS concrete filters
    are available the antibody is still written but flagged LOW-SPECIFICITY so the
    block-curator (and bid-qualifier matcher) can refuse a too-broad match."""
    filters = {}
    if vertical:
        filters["verticals"] = [str(vertical)]
    if client:
        filters["clients"] = [str(client)]
    service_line = ctx.get("service_line")
    if service_line:
        filters["service_lines"] = [str(service_line)]
    n_filters = len(filters)
    warnings = []
    if n_filters < MIN_ANTIBODY_FILTERS:
        warnings.append(f"LOW-SPECIFICITY antibody: only {n_filters} filter(s) "
                        f"(< MIN_ANTIBODY_FILTERS={MIN_ANTIBODY_FILTERS}); matcher must guard.")
    filters["min_filters_required"] = MIN_ANTIBODY_FILTERS
    filters["n_filters"] = n_filters
    filters["specificity_ok"] = n_filters >= MIN_ANTIBODY_FILTERS
    return filters, warnings


def emit_loss_antibody(bid_id: str, ctx: dict, dry_run: bool = False) -> str:
    """Write a DRAFT type=failure_mode block to 00_Inbox/from-dust/close_bid/ with
    valid SBAP frontmatter. Goes through normal block-curator triage — NEVER written
    to _brain_api directly."""
    loss_reason = ctx.get("loss_reason") or "unspecified (no loss_reason: in Decision Log)"
    vertical = ctx.get("vertical")
    client = ctx.get("client")
    applicable_to, warnings = build_antibody_applicable_to(vertical, client, ctx)

    run_id = f"close-loss-{bid_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    generated = utcnow()
    # SBAP frontmatter. source_agent must match ^[a-z0-9-]+$ → close-bid.
    fm = {
        "sbap_version": "1.0",
        "source_agent": "close-bid",
        "source_run_id": run_id,
        "generated": generated,
        "input_context_refs": [f"RFPs/.../{bid_id}/03 - Decision Log.md"],
        "output_type": "failure_mode",
        "target_path": "",  # review-only DRAFT — block-curator decides the canonical home
        "confidence": 0.5,
        "needs_review": True,
        "reasoning_summary": f"Loss antibody auto-drafted on close of {bid_id}. "
                             f"Specificity-guarded ({applicable_to['n_filters']} filters).",
    }
    body = {
        "type": "failure_mode",
        "key": f"{bid_id}__loss_antibody",
        "title": f"Loss antibody — {client or bid_id}: {str(loss_reason)[:60]}",
        "body": (f"Bid `{bid_id}` was LOST. Structured loss_reason: **{loss_reason}**.\n\n"
                 f"DRAFT antibody for block-curator review. When a future bid matches the "
                 f"specificity-guarded filters below, bid-qualifier docks the qualification "
                 f"score and cites this lost bid. Co-occurrence signal, not proof of causation."),
        "applicable_to": applicable_to,
        "evidence": [{"bid_id": bid_id, "outcome": "lost", "loss_reason": loss_reason,
                      "first_seen": generated[:10]}],
        "created": generated,
        "created_by": "close-bid.py (auto-draft on loss)",
        "schema_version": "1.1",
        "DRAFT": True,
    }
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    content = f"---\n{fm_text}\n---\n\n```json\n{json.dumps(body, indent=2)}\n```\n"

    if dry_run:
        msg = f"DRY would write loss antibody draft → 00_Inbox/from-dust/close_bid/{run_id}.md"
        return msg + ("  [" + "; ".join(warnings) + "]" if warnings else "")

    INBOX.mkdir(parents=True, exist_ok=True)
    out = INBOX / f"{run_id}.md"
    out.write_text(content)
    msg = f"LOSS ANTIBODY DRAFT → {out.relative_to(VAULT)} (block-curator will triage)"
    if warnings:
        msg += "  [" + "; ".join(warnings) + "]"
    return msg


# ─────────────────────────── orchestration ───────────────────────────

def run_canonical_view(dry_run: bool) -> None:
    if dry_run:
        print("DRY would run build_canonical_view.py")
        return
    here = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(here / "build_canonical_view.py")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ⚠ build_canonical_view.py failed: {r.stderr[:300]}", file=sys.stderr)
    else:
        print("  refold:", (r.stdout.strip().splitlines() or ["(no output)"])[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid_id")
    ap.add_argument("--dry-run", action="store_true")
    # Synthetic-test overrides (for bids not present in RFPs/):
    ap.add_argument("--outcome", choices=["won", "lost"])
    ap.add_argument("--vertical")
    ap.add_argument("--value-eur", type=float, dest="value_eur")
    ap.add_argument("--used-blocks", help="comma-separated block keys (overrides Decision Log)")
    ap.add_argument("--loss-reason")
    ap.add_argument("--client")
    args = ap.parse_args()

    bid_id = args.bid_id

    # Pause-aware: if the fleet is paused for migration, refuse to mutate the ledger.
    if (VAULT / "_agent_state" / "AUTOMATION_PAUSED").exists() and not args.dry_run:
        print("FLEET PAUSED (AUTOMATION_PAUSED) — close_bid refuses to mutate the ledger. "
              "Use --dry-run, or lift the pause.", file=sys.stderr)
        return 3

    bid_dir = find_bid_dir(bid_id)
    if bid_dir is not None:
        ctx = read_bid_context(bid_dir)
    else:
        ctx = {"outcome": None, "vertical": None, "value_eur": None,
               "used_blocks": [], "loss_reason": None, "client": None}

    # Apply synthetic overrides (do NOT mutate the live bid — these are CLI-only).
    if args.outcome:
        ctx["outcome"] = args.outcome
    if args.vertical is not None:
        ctx["vertical"] = args.vertical
    if args.value_eur is not None:
        ctx["value_eur"] = args.value_eur
    if args.used_blocks is not None:
        ctx["used_blocks"] = [b.strip() for b in args.used_blocks.split(",") if b.strip()]
    if args.loss_reason is not None:
        ctx["loss_reason"] = args.loss_reason
    if args.client is not None:
        ctx["client"] = args.client

    outcome = ctx["outcome"]
    if outcome not in ("won", "lost"):
        print(f"Bid {bid_id} stage is not Won/Lost (outcome={outcome}). Nothing to close. "
              f"Provide --outcome for a synthetic test.", file=sys.stderr)
        return 2

    used = ctx["used_blocks"]
    print(f"close_bid: {bid_id} outcome={outcome} vertical={ctx['vertical']} "
          f"value_eur={ctx['value_eur']} used_blocks={used}")

    if not used:
        print("  (no used_blocks confirmed — no ledger credit; recommended != used)")

    seen = triage.load_seen(LEDGER_AGENT)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for block_key in used:
        # Only credit blocks that exist as a SOURCE block (guard against typos).
        src, _ = load_source_block(block_key)
        if src is None:
            print(f"  ⚠ {block_key}: no source block under _agent_state/canonical/ — skipped")
            continue
        print("  " + append_outcome(bid_id, block_key, outcome, ctx["vertical"],
                                     ctx["value_eur"], date, seen, dry_run=args.dry_run))

    if outcome == "lost":
        print("  " + emit_loss_antibody(bid_id, ctx, dry_run=args.dry_run))

    run_canonical_view(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
