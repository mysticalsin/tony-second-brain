#!/usr/bin/env python3
"""grill_bid.py — Claude rival-bidder red-team driver.

Orchestrates the /grill-bid command non-interactively via claude -p subprocess
calls. Mirrors the adversarial pattern from grill-me-codex but uses Claude
(not Codex) — Codex token budget is exhausted.

Two skeptic personas per round:
  A — Rival vendor (best competitor, per ghost-brief.md or default Accenture)
  B — Procurement committee evaluator (RFP rubric, toughest possible read)

Output:
  <bid_folder>/redteam.md       — ranked objections + kill-shot questions + counters
  _relay/ISSUES.md              — unresolved KILLER/HIGH objections appended

Usage (from vault root):
    python3 build/tools/grill_bid.py "RFPs/Globex Cloud Migration"
    python3 build/tools/grill_bid.py "<bid>" --rounds 5 --model claude-sonnet-4-6
    python3 build/tools/grill_bid.py "<bid>" --dry-run   # print prompts, no API call
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
MAX_ROUNDS_DEFAULT = 3
MODEL_DEFAULT = "claude-haiku-4-5"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def claude_bin() -> str:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    raise RuntimeError("claude CLI not found")


def synthesize(prompt: str, model: str = MODEL_DEFAULT, timeout: int = 240) -> str:
    cb = claude_bin()
    result = subprocess.run(
        [cb, "-p", prompt, "--model", model,
         "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(VAULT),
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1",
             "ULTRON_VOICE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}): {result.stderr[:400]}"
        )
    return result.stdout.strip()


def safe_read(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    return path.read_text()[:max_chars]


# --------------------------------------------------------------------------- #
# Load bid inputs
# --------------------------------------------------------------------------- #

class BidInputs:
    def __init__(self, bid_path: Path):
        self.path = bid_path
        self.rfp_model: dict = {}
        self.win_recs: str = ""
        self.proposal_draft: str = ""
        self.ghost_brief: str = ""
        self.research: str = ""
        self.scorecard: str = ""
        self.missing: list[str] = []

    def load(self):
        def req(filename: str, attr: str, parse_json: bool = False):
            f = self.path / filename
            if not f.exists():
                self.missing.append(filename)
                return
            raw = f.read_text()
            if parse_json:
                setattr(self, attr, json.loads(raw))
            else:
                setattr(self, attr, raw[:6000])

        req("rfp-model.json", "rfp_model", parse_json=True)
        req("win-recs.md", "win_recs")
        req("02 - Proposal Draft.md", "proposal_draft")
        # Optional
        for fname, attr in [("ghost-brief.md", "ghost_brief"),
                             ("research.md", "research"),
                             ("scorecard.md", "scorecard")]:
            f = self.path / fname
            if f.exists():
                setattr(self, attr, f.read_text()[:4000])

        mandatory = {"rfp-model.json", "win-recs.md", "02 - Proposal Draft.md"}
        fatal = [m for m in self.missing if m in mandatory]
        if fatal:
            raise FileNotFoundError(
                f"Missing mandatory bid inputs: {fatal}\nIn: {self.path}"
            )

    @property
    def client(self) -> str:
        return self.rfp_model.get("client", "client")

    @property
    def scope(self) -> str:
        return self.rfp_model.get("scope", "")

    @property
    def eval_criteria_str(self) -> str:
        return "\n".join(
            f"- {c['name']} ({c.get('weight', '?')}%)"
            for c in self.rfp_model.get("eval_criteria", [])
        )

    @property
    def best_competitor(self) -> str:
        if self.ghost_brief:
            # Try to extract first named competitor from ghost brief
            m = re.search(r"###\s+([A-Za-z ]+)\n", self.ghost_brief)
            if m:
                return m.group(1).strip()
        return "Accenture"


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #

RIVAL_PROMPT = """You are the lead bid strategist for {competitor}, submitting a competing proposal for this SAP AMS RFP.

## Your context
You have just reviewed your competitor's (the proposing firm's) proposal and supporting materials for this bid.

## RFP
Client: {client}
Scope: {scope}
Evaluation criteria:
{eval_criteria}

## The proposing firm's proposal (what you are attacking)
{proposal_draft}

## The proposing firm's known win themes / gaps (from their internal win-recs doc)
{win_recs}

---

Your job: find every argument you would make to the procurement committee about why YOUR proposal is stronger and the proposing firm's is weaker.

For each objection, produce a structured entry:
**OBJECTION [N]:**
- Claim: (1 sentence — the procurement argument)
- Criterion targeted: (from eval_criteria list)
- Evidence from proposal: (specific gap or absence)
- Kill-shot question: (the hardest single question you'd want the evaluator to ask the proposing firm)
- Counter the proposing firm should pre-empt: (1 sentence — what they'd need to add to neutralize this)

Produce 5-7 objections. Be specific. Name real gaps. No padding.
"""

PROCUREMENT_PROMPT = """You are the procurement committee evaluator for {client}, scoring incoming proposals.

## Your context
You are NOT being fair. You are running the toughest possible evaluation pass against the proposing firm's proposal.

## RFP rubric
Client: {client}
Evaluation criteria:
{eval_criteria}

## The proposing firm's proposal (what you are evaluating)
{proposal_draft}

## Known requirements from rfp-model.json
Mandatory clauses: {mandatory_clauses}

---

For each finding, produce a structured entry:
**GAP [N]:**
- Finding: (1 sentence)
- Criterion affected: (from eval_criteria list)
- Severity: KILLER (removes from consideration) / HIGH (major point loss) / MED (recoverable)
- What needs to be in the proposal to fix this: (specific, actionable)

Produce 5-8 findings. Be the hardest evaluator the proposing firm will ever face.
"""

ARBITRATOR_PROMPT = """You are the bid manager reviewing two red-team passes on the proposing firm's proposal.

## Round {round_n} inputs

### Rival vendor (competitor: {competitor}) found these objections:
{rival_output}

### Procurement committee found these gaps:
{procurement_output}

## Prior rounds summary (what's already known)
{prior_summary}

---

Your job:
1. DEDUPLICATE — merge overlapping objections into a unified ranked list.
2. RANK — KILLER first, then HIGH, then MED.
3. TAG each item RESOLVED (proposal already addresses it adequately) or OPEN (proposal does not).
4. SUMMARIZE — write a concise summary of this round: what's new, what's resolved, what remains.
5. CONVERGENCE CHECK — state YES or NO: did this round surface any NEW material objections vs prior rounds?

Produce your output in this structure:

## Round {round_n} — Unified findings

### KILLERS
| # | Objection | Criterion | Kill-shot Q | Counter | Status |
|---|---|---|---|---|---|
...

### HIGH
| # | Objection | Criterion | Kill-shot Q | Counter | Status |
|---|---|---|---|---|---|
...

### MED
| # | Objection | Criterion | Counter | Status |
|---|---|---|---|---|
...

## Round summary
(3-5 sentences: new objections this round, items resolved, items still open)

## Convergence verdict
NEW_MATERIAL: YES / NO
"""


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def run_grill(
    bid: BidInputs,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
    model: str = MODEL_DEFAULT,
    dry_run: bool = False,
) -> dict:
    """
    Returns {
        "rounds": [...],        # per-round outputs
        "converged": bool,
        "open_items": [...],    # final OPEN objections
        "round_log": str,       # raw markdown for redteam.md round section
    }
    """
    rounds: list[dict] = []
    prior_summary = "No prior rounds."
    converged = False

    for round_n in range(1, max_rounds + 1):
        print(f"\n[grill-bid] Round {round_n}/{max_rounds}...", file=sys.stderr)

        # Persona A — Rival vendor
        rival_prompt = RIVAL_PROMPT.format(
            competitor=bid.best_competitor,
            client=bid.client,
            scope=bid.scope,
            eval_criteria=bid.eval_criteria_str,
            proposal_draft=bid.proposal_draft[:3000],
            win_recs=bid.win_recs[:2000],
        )

        # Persona B — Procurement committee
        procurement_prompt = PROCUREMENT_PROMPT.format(
            client=bid.client,
            eval_criteria=bid.eval_criteria_str,
            proposal_draft=bid.proposal_draft[:3000],
            mandatory_clauses=json.dumps(
                bid.rfp_model.get("mandatory_clauses", []), indent=2
            )[:1500],
        )

        if dry_run:
            print(f"\n--- RIVAL PROMPT (Round {round_n}) ---")
            print(rival_prompt[:800])
            print(f"\n--- PROCUREMENT PROMPT (Round {round_n}) ---")
            print(procurement_prompt[:800])
            print("--- END DRY RUN ---")
            return {"rounds": [], "converged": False, "open_items": [], "round_log": ""}

        print(f"  [grill-bid] Persona A (rival vendor: {bid.best_competitor})...",
              file=sys.stderr)
        rival_out = synthesize(rival_prompt, model=model)

        print(f"  [grill-bid] Persona B (procurement committee)...", file=sys.stderr)
        proc_out = synthesize(procurement_prompt, model=model)

        # Arbitrator
        arb_prompt = ARBITRATOR_PROMPT.format(
            round_n=round_n,
            competitor=bid.best_competitor,
            rival_output=rival_out[:3000],
            procurement_output=proc_out[:3000],
            prior_summary=prior_summary,
        )
        print(f"  [grill-bid] Arbitrator pass...", file=sys.stderr)
        arb_out = synthesize(arb_prompt, model=model)

        round_data = {
            "round": round_n,
            "timestamp": utcnow(),
            "rival": rival_out,
            "procurement": proc_out,
            "arbitration": arb_out,
        }
        rounds.append(round_data)

        # Check convergence
        converged_match = re.search(r"NEW_MATERIAL:\s*(YES|NO)", arb_out, re.I)
        new_material = converged_match.group(1).upper() if converged_match else "YES"
        if new_material == "NO":
            print(f"  [grill-bid] Convergence at round {round_n}", file=sys.stderr)
            converged = True
            break

        # Update prior summary for next round
        summary_match = re.search(r"## Round summary\n(.*?)(?:\n##|$)", arb_out, re.S)
        prior_summary = summary_match.group(1).strip() if summary_match else arb_out[-500:]

    # Extract final OPEN items from last arbitration
    last_arb = rounds[-1]["arbitration"] if rounds else ""
    open_items = extract_open_items(last_arb)

    return {
        "rounds": rounds,
        "converged": converged,
        "open_items": open_items,
        "round_log": build_round_log(rounds),
    }


def extract_open_items(arb_output: str) -> list[dict]:
    """Extract OPEN rows from the arbitration markdown table."""
    items = []
    for line in arb_output.splitlines():
        if "OPEN" in line and "|" in line:
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) >= 3:
                objection = cols[1] if len(cols) > 1 else line
                criterion = cols[2] if len(cols) > 2 else ""
                severity = "HIGH"
                if "KILLER" in arb_output[:arb_output.index(line)] if line in arb_output else "":
                    severity = "KILLER"
                items.append({
                    "objection": objection,
                    "criterion": criterion,
                    "severity": severity,
                })
    return items


def build_round_log(rounds: list[dict]) -> str:
    parts = []
    for r in rounds:
        parts.append(
            f"### Round {r['round']} — {r['timestamp']}\n\n"
            f"**Rival vendor objections:**\n\n{r['rival']}\n\n"
            f"**Procurement committee findings:**\n\n{r['procurement']}\n\n"
            f"**Arbitration:**\n\n{r['arbitration']}\n"
        )
    return "\n\n---\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Write redteam.md
# --------------------------------------------------------------------------- #

def write_redteam(bid: BidInputs, result: dict, max_rounds: int) -> Path:
    open_count = len(result["open_items"])
    converged = result["converged"]
    rounds_run = len(result["rounds"])

    # Extract final arbitration for top-level summary
    final_arb = result["rounds"][-1]["arbitration"] if result["rounds"] else ""

    fm = (
        "---\n"
        f"type: redteam\n"
        f"generated: {utcnow()}\n"
        f"bid: {bid.path.name}\n"
        f"rounds_run: {rounds_run}\n"
        f"max_rounds: {max_rounds}\n"
        f"converged: {str(converged).lower()}\n"
        f"open_objections: {open_count}\n"
        "tags: [redteam, competitive]\n"
        "---\n\n"
    )

    body = f"""# Red-Team — {bid.client} / {bid.path.name}

## Summary

- **Rounds run:** {rounds_run} / {max_rounds}
- **Converged:** {"Yes — no new material objections in final round" if converged else "No — MAX_ROUNDS reached"}
- **Open objections remaining:** {open_count}
- **Rival vendor persona:** {bid.best_competitor}
- **Inputs loaded:** rfp-model.json, win-recs.md, 02 - Proposal Draft.md{"+ ghost-brief.md" if bid.ghost_brief else ""}{"+ research.md" if bid.research else ""}{"+ scorecard.md" if bid.scorecard else ""}

---

## Final ranked objections (from last arbitration pass)

{final_arb}

---

## Full round log

{result["round_log"]}

---

## Recommended next actions

Review the KILLER and HIGH items above. Typical sequence:
1. Fix KILLERs first (they disqualify — no score recovery possible otherwise)
2. Address HIGH items in order of criterion weight (from rfp-model.json)
3. MED items: fix if time allows; flag in proposal where they're acknowledged
4. Run `/grill-bid` again after material edits to re-test

Then hand off the findings with a human reviewer for the final pass.
"""

    content = fm + body
    out = bid.path / "redteam.md"
    out.write_text(content)
    print(f"[grill-bid] Written: {out}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- #
# Append to _relay/ISSUES.md
# --------------------------------------------------------------------------- #

def append_issues(bid: BidInputs, open_items: list[dict]):
    """Append OPEN KILLER/HIGH objections to _relay/ISSUES.md."""
    issues_path = VAULT / "_relay" / "ISSUES.md"
    if not issues_path.parent.exists():
        print(f"[grill-bid] _relay/ not found — skipping ISSUES.md append",
              file=sys.stderr)
        return

    high_items = [i for i in open_items
                  if i.get("severity", "").upper() in ("KILLER", "HIGH")]
    if not high_items:
        print("[grill-bid] No KILLER/HIGH open items — ISSUES.md unchanged",
              file=sys.stderr)
        return

    # Generate sequential IDs from existing content
    existing = issues_path.read_text() if issues_path.exists() else ""
    existing_ids = re.findall(r"\|\s*(GT-\d+)\s*\|", existing)
    next_id_n = (max(int(x.split("-")[1]) for x in existing_ids) + 1
                 if existing_ids else 1)

    new_rows = []
    for item in high_items:
        issue_id = f"GT-{next_id_n:03d}"
        next_id_n += 1
        row = (f"| {issue_id} | bid | {item.get('severity', 'HIGH')} | "
               f"{item.get('objection', '?')[:80]} | "
               f"Fix `{item.get('criterion', '?')}` in proposal (from /grill-bid {bid.path.name}) |")
        new_rows.append(row)

    append_block = (
        f"\n\n<!-- grill-bid {bid.path.name} {utcnow()} -->\n"
        + "\n".join(new_rows)
    )

    if issues_path.exists():
        issues_path.write_text(existing.rstrip() + append_block)
    else:
        issues_path.write_text(
            "# ISSUES\n\n| ID | Dimension | Severity | Symptom | Next action |\n"
            "|---|---|---|---|---|\n"
            + "\n".join(new_rows)
        )
    print(f"[grill-bid] Appended {len(new_rows)} items to {issues_path}",
          file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def resolve_bid_path(slug: str, vault: Path) -> Path:
    """Resolve bid slug or path to an absolute path."""
    p = Path(slug)
    if p.is_absolute() and p.exists():
        return p
    candidate = vault / slug
    if candidate.exists():
        return candidate.resolve()
    # Search for it
    found = list(vault.rglob(f"*{slug}*"))
    dirs = [f for f in found if f.is_dir()]
    if dirs:
        return dirs[0]
    raise FileNotFoundError(f"Bid path not found: {slug}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Adversarial red-team for a bid folder using Claude skeptic personas."
    )
    ap.add_argument("bid", help="Bid folder path or slug")
    ap.add_argument("--rounds", type=int, default=MAX_ROUNDS_DEFAULT,
                    help=f"Max rounds (default: {MAX_ROUNDS_DEFAULT})")
    ap.add_argument("--model", default=MODEL_DEFAULT,
                    help=f"Claude model (default: {MODEL_DEFAULT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print prompts but don't call claude or write files")
    ap.add_argument("--vault", default=None,
                    help="Vault root (default: $VAULT or cwd)")
    args = ap.parse_args()

    global VAULT
    vault = Path(args.vault).resolve() if args.vault else VAULT
    VAULT = vault

    bid_path = resolve_bid_path(args.bid, vault)
    print(f"[grill-bid] Bid path: {bid_path}", file=sys.stderr)

    bid = BidInputs(bid_path)
    bid.load()
    print(f"[grill-bid] Inputs: {[f for f in ['rfp-model.json', 'win-recs.md', '02 - Proposal Draft.md', 'ghost-brief.md', 'research.md', 'scorecard.md'] if (bid_path / f).exists()]}", file=sys.stderr)
    print(f"[grill-bid] Model: {args.model} | Max rounds: {args.rounds}", file=sys.stderr)

    result = run_grill(
        bid,
        max_rounds=args.rounds,
        model=args.model,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0

    out = write_redteam(bid, result, max_rounds=args.rounds)
    append_issues(bid, result["open_items"])

    converged_str = "converged" if result["converged"] else "MAX_ROUNDS reached"
    open_str = f"{len(result['open_items'])} open items appended to ISSUES.md"
    print(f"\nredteam.md: {out}")
    print(f"Status: {converged_str} in {len(result['rounds'])} rounds — {open_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
