#!/usr/bin/env python3
"""winrecs_autofix.py — Parse win-recs.md's 10-factor table + Top 3 moves.

For each non-✅ factor, write a DRAFT-REQUEST note into
  00_Inbox/from-dust/<owning-agent>/<date>-<bid>-<factor-slug>.md
with valid SBAP frontmatter and confidence < 0.85 (triage HOLDS for review).

Owner mapping (gap-type → agent):
  narrative / section / deck / story → rfp-drafter
  cost-of-inaction / ROI / payback / pricing / commercial → financial-pulse
  canonical block / reference / pattern / block → block-curator

Usage (from vault root):
  python build/tools/winrecs_autofix.py "<bid-path>"
  python build/tools/winrecs_autofix.py "RFPs/Globex Cloud Migration"

Flags:
  --dry-run   Print what would be written; write nothing.
  --confidence <float>   Override default 0.60 (must stay < 0.85 to ensure triage HOLDS).
"""
from __future__ import annotations
import argparse, json, os, re, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()

# ── SBAP owner-mapping ──────────────────────────────────────────────────────
# Each entry: (regex pattern on factor text, owning_agent, target_path_template)
# target_path_template receives: bid_rel (relative bid path from vault root),
#   factor_slug (slugified factor name), bid_name (bid directory name)
OWNER_MAP: list[tuple[str, str, str]] = [
    # Cost-of-inaction / ROI / payback / pricing math → financial-pulse
    (r"cost.of.inaction|roi|payback|pricing|commercial|rate.card|financial|budget|revenue|spend",
     "financial-pulse",
     "{bid_rel}/05 - Financials.md"),
    # Canonical blocks / references / patterns → block-curator
    (r"canonical.block|reference|pattern|block.curator|block|case.study|proof.point",
     "block-curator",
     "{bid_rel}/06 - References.md"),
    # Everything else (narrative, section, governance, deck) → rfp-drafter
    (r".*",
     "rfp-drafter",
     "{bid_rel}/02 - Proposal Draft.md"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    """Simple ASCII slug."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:48]


def owner_for_factor(factor_name: str, evidence: str) -> tuple[str, str]:
    """Return (agent, target_path_template) for a given factor name + evidence text."""
    combined = (factor_name + " " + evidence).lower()
    for pattern, agent, tpl in OWNER_MAP:
        if re.search(pattern, combined):
            return agent, tpl
    return "rfp-drafter", "{bid_rel}/02 - Proposal Draft.md"


# ── Score symbols (✅, ⚠️ is U+26A0+FE0F variation, ❌) ──────────────────
# ⚠️ is a multi-codepoint sequence in Python; match it as a raw Unicode chunk.
_SCORE_SYMBOLS = ("✅", "⚠️", "⚠", "❌")

def _extract_score(cell: str) -> str:
    """Extract the score symbol from a table cell, normalising ⚠+FE0F → ⚠️."""
    cell = cell.strip()
    for sym in _SCORE_SYMBOLS:
        if sym in cell:
            return "⚠️" if "⚠" in sym else sym
    return ""


# ── Parser for the 10-factor table ─────────────────────────────────────────
# Matches: | N | **Factor name** | ✅/⚠️/❌ | Evidence text |
# NOTE: score column matched as [^\|]+ to avoid emoji encoding issues
FACTOR_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*([^\|]+?)\s*\|\s*(.+?)\|?\s*$"
)
# Also match bare symbol without **: | N | Factor name | ✅ | ...
FACTOR_ROW_BARE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*(.+?)\s*\|\s*([^\|]+?)\s*\|\s*(.+?)\|?\s*$"
)
# Top 3 moves block header
TOP3_RE = re.compile(r"^##\s+Top 3 moves", re.I)
NUMBERED_MOVE_RE = re.compile(r"^\*\*(\d+)\.\s+(.+?)\*\*")


def parse_win_recs(text: str) -> tuple[list[dict], list[dict]]:
    """Parse win-recs.md.
    Returns:
      factors: list of {num, name, score, evidence}
      top3: list of {title, body}
    """
    factors: list[dict] = []
    top3: list[dict] = []

    in_table = False
    in_top3 = False
    current_move: dict | None = None

    for line in text.splitlines():
        # Detect table region
        if "| # |" in line or "| Factor |" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("|---"):
            continue
        if in_table and line.strip().startswith("|"):
            m = FACTOR_ROW_RE.match(line)
            if not m:
                m = FACTOR_ROW_BARE_RE.match(line)
            if m:
                name = m.group(1).strip()
                score = _extract_score(m.group(2))
                evidence = m.group(3).strip()
                # Skip rows that don't have a score symbol (e.g. header row caught by bare regex)
                if not score:
                    continue
                # Extract factor number from line (before first cell)
                num_m = re.match(r"^\|\s*(\d+)\s*\|", line)
                num = int(num_m.group(1)) if num_m else len(factors) + 1
                factors.append({"num": num, "name": name, "score": score, "evidence": evidence})
                continue
        elif in_table and not line.strip().startswith("|"):
            in_table = False

        # Detect Top 3 section
        if TOP3_RE.match(line):
            in_top3 = True
            continue
        if in_top3:
            if line.startswith("##"):
                in_top3 = False
                if current_move:
                    top3.append(current_move)
                    current_move = None
                continue
            mm = NUMBERED_MOVE_RE.match(line)
            if mm:
                if current_move:
                    top3.append(current_move)
                current_move = {"num": int(mm.group(1)), "title": mm.group(2).strip(), "body": ""}
            elif current_move is not None:
                current_move["body"] = (current_move["body"] + "\n" + line).strip()

    if current_move:
        top3.append(current_move)

    return factors, top3


def build_draft_request(
    *,
    bid: Path,
    bid_rel: str,
    factor: dict,
    top3_body: str,
    agent: str,
    target_path: str,
    confidence: float,
    run_id: str,
    date_str: str,
) -> tuple[str, str]:
    """Build (filename, file_content) for one SBAP draft-request note."""
    factor_slug = slugify(factor["name"])
    score_label = {"✅": "resolved", "⚠️": "partial", "❌": "missing"}.get(factor["score"], "unknown")
    filename = f"{date_str}-{bid.name}-factor{factor['num']:02d}-{factor_slug}.md"

    gap_summary = (
        f"Factor {factor['num']} ({factor['name']}) is scored **{factor['score']} ({score_label})**.\n\n"
        f"**Current evidence:** {factor['evidence']}\n"
    )
    if top3_body:
        gap_summary += f"\n**Top-3 move context (from win-recs.md):**\n{top3_body}\n"

    content = (
        f"---\n"
        f'sbap_version: "1.0"\n'
        f"source_agent: rfp-pipeline\n"
        f'source_run_id: "{run_id}"\n'
        f'generated: "{utcnow()}"\n'
        f"input_context_refs:\n"
        f'  - "{bid_rel}/win-recs.md"\n'
        f'  - "{bid_rel}/rfp-model.json"\n'
        f"output_type: proposal_draft\n"
        f'target_path: "{target_path}"\n'
        f"confidence: {confidence:.2f}\n"
        f"needs_review: true\n"
        f'reasoning_summary: "Draft-request for factor {factor["num"]} ({factor["name"]}) '
        f'scored {factor["score"]} in win-recs.md — routed to {agent} to address gap."\n'
        f"learnings:\n"
        f'  - "Factor {factor["num"]} gap in {bid.name}: {factor["evidence"][:60]}"\n'
        f"---\n\n"
        f"# DRAFT REQUEST — Factor {factor['num']}: {factor['name']}\n\n"
        f"> **Bid:** `{bid_rel}`  \n"
        f"> **Assigned to:** @{agent}  \n"
        f"> **Win-recs score:** {factor['score']} ({score_label})  \n"
        f"> **Confidence:** {confidence:.2f} (held for Tony review — not auto-promoted)\n\n"
        f"## Gap description\n\n"
        f"{gap_summary}\n"
        f"## What to draft\n\n"
        f"Address the gap above in the relevant section of the proposal. "
        f"Specifically:\n\n"
    )

    # Add factor-specific guidance based on the score and name
    name_lc = factor["name"].lower()
    evidence_lc = factor["evidence"].lower()
    if "cost" in name_lc or "inaction" in name_lc or "roi" in name_lc or "payback" in name_lc:
        content += (
            "- Quantify the cost-of-inaction: estimate €/month of unmanaged risk\n"
            "- Provide a payback period anchor (months to ROI)\n"
            "- This number must appear BEFORE any rate card\n"
        )
    elif "reference" in name_lc or "case study" in name_lc or "proof" in name_lc or "metric" in name_lc:
        content += (
            "- Pull at least one quantified metric from the cited reference\n"
            "- Format: [reference name] → [metric: value] — attach to the relevant proposal section\n"
            "- If no metric is available, flag for account team to retrieve\n"
        )
    elif "objection" in name_lc or "boutique" in name_lc or "risk" in name_lc:
        content += (
            "- Add a named objection-killing element (e.g. Big 4 alumni, insurance-backed guarantee)\n"
            "- Place in first five slides / executive summary section\n"
        )
    elif "narrative" in name_lc or "story" in name_lc or "hook" in name_lc:
        content += (
            "- Open with a shocking stat or provocative question\n"
            "- Follow the Hook → Problem → Vision → Proof structure\n"
            "- Ensure the win-theme is findable within 60 seconds\n"
        )
    elif "governance" in name_lc or "council" in name_lc or "bilateral" in name_lc:
        content += (
            "- Name the governance body (e.g. Innovation Council)\n"
            "- List 3 bilateral trigger conditions (e.g. client timeline shift, licensing change, volume threshold)\n"
            "- Add to Section 4 (contractual / engagement model)\n"
        )
    else:
        content += (
            "- Address the specific gap described above\n"
            "- Back claims with quantified proof (metrics, case studies, certifications)\n"
            "- Ensure the section is findable in the evaluator flow\n"
        )

    content += (
        f"\n## Target file\n\n"
        f"`{target_path}`\n\n"
        f"## How to clear this request\n\n"
        f"1. Draft the section in the target file\n"
        f"2. Tony reviews via `/dust-resolve`\n"
        f"3. If accepted, re-run win-recs to confirm the factor improves to ✅\n"
    )

    return filename, content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bid_path")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written; write nothing")
    ap.add_argument("--confidence", type=float, default=0.60,
                    help="SBAP confidence for all draft-requests (must stay < 0.85; default 0.60)")
    args = ap.parse_args()

    if args.confidence >= 0.85:
        print(f"ERROR: --confidence must be < 0.85 (got {args.confidence}); "
              "draft-requests must be held for Tony review, not auto-promoted.", file=sys.stderr)
        return 2

    bid = (VAULT / args.bid_path).resolve()
    if not bid.is_dir():
        print(f"ERROR: bid not found: {bid}", file=sys.stderr)
        return 2

    wr_file = bid / "win-recs.md"
    if not wr_file.exists():
        print(f"ERROR: win-recs.md not found in {bid}", file=sys.stderr)
        return 2

    bid_rel = str(bid.relative_to(VAULT))
    run_id = f"{utcnow()}-winrecs-autofix-{bid.name}-{uuid.uuid4().hex[:8]}"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    text = wr_file.read_text(encoding="utf-8")
    factors, top3 = parse_win_recs(text)

    if not factors:
        print("winrecs_autofix: no factors parsed from win-recs.md — check table format")
        return 1

    # Build a lookup: factor_num → top3 body
    top3_by_factor: dict[int, str] = {}
    for move in top3:
        # Top 3 moves reference specific factors in parentheses like "(Factors 5 + 8)"
        body_text = move.get("body", "")
        factor_refs = re.findall(r"[Ff]actors?\s*(\d+)", move.get("title", "") + " " + body_text)
        for ref in factor_refs:
            top3_by_factor[int(ref)] = f"**Move {move['num']}: {move['title']}**\n{body_text}"

    non_green = [f for f in factors if f["score"] != "✅"]
    print(f"=== winrecs_autofix · {bid_rel} ===")
    print(f"   Factors parsed: {len(factors)} total, {len(non_green)} non-✅")

    if not non_green:
        print("   All factors are ✅ — no draft-requests needed.")
        return 0

    written: list[str] = []
    for factor in non_green:
        agent, tpl = owner_for_factor(factor["name"], factor["evidence"])
        target_path = tpl.format(bid_rel=bid_rel, factor_slug=slugify(factor["name"]),
                                  bid_name=bid.name)
        top3_body = top3_by_factor.get(factor["num"], "")

        filename, content = build_draft_request(
            bid=bid,
            bid_rel=bid_rel,
            factor=factor,
            top3_body=top3_body,
            agent=agent,
            target_path=target_path,
            confidence=args.confidence,
            run_id=run_id,
            date_str=date_str,
        )

        dest_dir = VAULT / "00_Inbox" / "from-dust" / agent
        dest_file = dest_dir / filename

        if args.dry_run:
            print(f"   DRY-RUN: would write {dest_file.relative_to(VAULT)}")
            print(f"           factor={factor['num']} '{factor['name']}' {factor['score']} → @{agent}")
            print(f"           target_path={target_path}")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(content, encoding="utf-8")
            written.append(str(dest_file.relative_to(VAULT)))
            print(f"   WROTE: {dest_file.relative_to(VAULT)}")
            print(f"          factor={factor['num']} '{factor['name']}' {factor['score']} → @{agent}")
            print(f"          target_path={target_path}")

    if not args.dry_run:
        print(f"   {len(written)} draft-request(s) queued (conf={args.confidence:.2f} → held by triage)")
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
